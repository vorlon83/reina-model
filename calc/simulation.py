from collections import namedtuple
import numpy as np
import pandas as pd

from . import calcfunc
from calc.datasets import get_population_for_area
from utils.perf import PerfCounter
from variables import set_variable, get_variable
import numba as nb


random_pool_spec = [
    ('idx', nb.int32),
    ('data', nb.float64[:]),
]

@nb.jitclass(random_pool_spec)
class RandomPool:
    def __init__(self):
        np.random.seed(1234)
        self.data = np.random.random_sample(50000)
        self.idx = 0

    def get(self):
        return np.random.random()

        out = self.data[self.idx]
        self.idx += 1
        if self.idx == self.data.size:
            self.idx = 0
        return out

    def chance(self, p):
        val = self.get()
        return val < p


person_spec = [
    ('age', nb.int8),
    ('immunity', nb.int8),
    ('infected', nb.int8),
    ('incubation_days_left', nb.int8),
    ('symptomatic_days_left', nb.int8),
    ('hospital_days_left', nb.int8),
    ('icu_days_left', nb.int8),
]


@nb.jitclass(person_spec)
class Person:
    def __init__(self, age):
        self.age = age
        self.infected = 0
        self.immunity = 0
        self.incubation_days_left = 0
        self.symptomatic_days_left = 0
        self.hospital_days_left = 0
        self.icu_days_left = 0

    def expose(self, context):
        if self.infected or self.immunity:
            return
        if context.random.chance(0.015):
            self.infect(context)

    def infect(self, context):
        self.incubation_days_left = 5
        self.infected = 1
        context.pop.infect()

    def recover(self, context):
        self.infected = 0
        self.immunity = 1
        context.pop.recover()

    def hospitalize(self, context):
        ICU_CHANCE = 0.25
        if context.random.chance(ICU_CHANCE):
            if not context.hc_cap.to_icu():
                # If no ICU units are available, ...
                self.die(context)
                return
            self.icu_days_left = 14  # 14-21?
        else:
            if not context.hc_cap.hospitalize():
                # If no beds are available, 20 % chance to die.
                if context.random.chance(0.20):
                    self.die(context)
                    return
                else:
                    self.recover(context)
                    return
            self.hospital_days_left = 14

        context.pop.hospitalize()

    def release_from_hospital(self, context, from_icu):
        context.pop.release_from_hospital()
        if from_icu:
            death_chance = 0.2
            context.hc_cap.release_from_icu()
        else:
            death_chance = 0.1
            context.hc_cap.release()

        if context.random.chance(death_chance):
            self.die(context)
        else:
            self.recover(context)

    def die(self, context):
        self.infected = 0
        self.immunity = 1
        context.pop.die()

    def expose_others(self, context, nr_contacts):
        people = context.people
        for i in range(nr_contacts):
            exposee_idx = int(context.random.get() * len(people))
            people[exposee_idx].expose(context)

    def advance(self, context):
        if self.incubation_days_left:
            self.incubation_days_left -= 1
            if self.incubation_days_left == 0:
                self.symptomatic_days_left = 7
            self.expose_others(context, 15)
            return

        if self.symptomatic_days_left:
            self.symptomatic_days_left -= 1
            self.expose_others(context, 7)
            if self.symptomatic_days_left == 0:
                HOSPITALIZATION_CHANCE = 0.15
                if context.random.chance(HOSPITALIZATION_CHANCE):
                    self.hospitalize(context)
                else:
                    self.recover(context)
            return

        if self.hospital_days_left:
            self.expose_others(context, 2)
            self.hospital_days_left -= 1
            if self.hospital_days_left == 0:
                self.release_from_hospital(context, from_icu=False)
            return

        if self.icu_days_left:
            self.icu_days_left -= 1
            if self.icu_days_left == 0:
                self.release_from_hospital(context, from_icu=True)
            return

hc_cap_spec = [
    ('beds', nb.int32),
    ('icu_units', nb.int32),
    ('available_beds', nb.int32),
    ('available_icu_units', nb.int32),
]


@nb.jitclass(hc_cap_spec)
class HealthcareCapacity:
    def __init__(self, beds, icu_units):
        self.beds = beds
        self.icu_units = icu_units
        self.available_beds = beds
        self.available_icu_units = icu_units

    def hospitalize(self):
        if self.available_beds == 0:
            return False
        self.available_beds -= 1
        return True

    def release(self):
        self.available_beds += 1
        assert self.available_beds <= self.beds

    def to_icu(self):
        if self.available_icu_units == 0:
            return False
        self.available_icu_units -= 1
        return True

    def release_from_icu(self):
        self.available_icu_units += 1
        assert self.available_icu_units <= self.icu_units


disease_params_spec = [
    ('age', nb.int8),
    ('needs_hospital_ratio', nb.float32),
    ('needs_icu_ratio', nb.float32),
]

@nb.jitclass(disease_params_spec)
class DiseaseParams:
    def __init__(self, age, needs_hospital_ratio, needs_icu_ratio):
        self.age = age
        self.needs_hospital_ratio = needs_hospital_ratio
        self.needs_icu_ratio = needs_icu_ratio


ModelState = namedtuple('ModelState', [
    'susceptible', 'infected', 'hospitalized', 'dead', 'recovered', 'available_hospital_beds', 'available_icu_units'
])


population_spec = [
    ('infected', nb.int32),
    ('hospitalized', nb.int32),
    ('dead', nb.int32),
    ('susceptible', nb.int32),
    ('recovered', nb.int32),
]

@nb.jitclass(population_spec)
class Population:
    def __init__(self, total):
        self.susceptible = total
        self.infected = 0
        self.recovered = 0
        self.hospitalized = 0
        self.dead = 0

    def infect(self):
        self.susceptible -= 1
        self.infected += 1

    def recover(self):
        self.infected -= 1
        self.recovered += 1

    def hospitalize(self):
        self.hospitalized += 1

    def release_from_hospital(self):
        self.hospitalized -= 1

    def die(self):
        self.infected -= 1
        self.dead += 1


context_spec = [
    ('pop', Population.class_type.instance_type),
    ('hc_cap', HealthcareCapacity.class_type.instance_type),
    ('random', RandomPool.class_type.instance_type),
    ('people', nb.types.ListType(Person.class_type.instance_type)),
]

@nb.jitclass(context_spec)
class Context:
    def __init__(self, pop, people, hc_cap):
        self.pop = pop
        self.people = people
        self.hc_cap = hc_cap
        self.random = RandomPool()

    def generate_state(self):
        p = self.pop
        hc = self.hc_cap
        s = ModelState(
            infected=p.infected, susceptible=p.susceptible,
            recovered=p.recovered, hospitalized=p.hospitalized,
            dead=p.dead,
            available_icu_units=hc.available_icu_units,
            available_hospital_beds=hc.available_beds,
        )
        return s


@nb.jit(nopython=True)
def create_population(ages, counts):
    pop = nb.typed.List()
    for age, count in zip(ages, counts):
        for i in range(count):
            pop.append(Person(age))
    return pop


@nb.jit(nopython=True)
def iterate_day(context):
    people = context.people
    for person in people:
        if not person.infected:
            continue

        person.advance(context)


@nb.jit(nopython=True)
def run_model(context, simulation_days):
    state_history = nb.typed.List()
    state_history.append(context.generate_state())
    for day in range(simulation_days):
        print(day)
        iterate_day(context)
        state_history.append(context.generate_state())

    return state_history


@calcfunc(
    variables=['simulation_days']
)
def simulate_individuals(variables):
    pc = PerfCounter()
    df = get_population_for_area()
    all_sexes = df.sum(axis=1)

    people = create_population(all_sexes.index.values, all_sexes.values)
    pop = Population(all_sexes.sum())
    hc_cap = HealthcareCapacity(5000, 300)
    context = Context(pop, people, hc_cap)

    # Initial infection
    for i in range(100):
        idx = int(context.random.get() * len(people))
        people[idx].infect(context)

    dis_params = nb.typed.List()
    for age in all_sexes.index.values:
        dis_params.append(DiseaseParams(age, needs_hospital_ratio=0.15, needs_icu_ratio=0.05))

    pc.display('after init')
    states = run_model(context, 100)  # variables['simulation_days'])
    pc.display('after run1')
    
    recs = []
    attrs = ['infected', 'susceptible', 'hospitalized', 'dead', 'recovered', 'available_hospital_beds', 'available_icu_units']
    for s in states:
        d = {k: getattr(s, k) for k in attrs}
        recs.append(d)

    return pd.DataFrame.from_records(recs, index=range(0, len(recs)))


if __name__ == '__main__':
    df = simulate_individuals()
    df['total'] = df.infected + df.recovered + df.susceptible + df.dead
    df['cfr'] = df.dead / (df.infected + df.recovered)
    pd.set_option('display.max_rows', 200)
    print(df)
