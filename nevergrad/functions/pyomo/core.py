# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
from functools import partial
import numpy as np
import pyomo.environ as pyomo
import nevergrad.common.typing as tp
from nevergrad.parametrization import parameter as p
from .. import base


ParamDict = tp.Dict[str, p.Parameter]


def _convert_to_ng_name(pyomo_var_key: tp.Any) -> str:
    if isinstance(pyomo_var_key, str):
        return '"' + str(pyomo_var_key) + '"'
    else:
        return str(pyomo_var_key)


def _make_pyomo_range_set_to_parametrization(
    domain: pyomo.RangeSet, params: ParamDict, params_name: str
) -> ParamDict:
    # https://pyomo.readthedocs.io/en/stable/pyomo_modeling_components/Sets.html
    # Refer to the implementation in pyomo/core/base/set.py
    ranges = list(domain.ranges())
    num_ranges = len(ranges)
    if num_ranges == 1 and (ranges[0].step in [-1, 0, 1]):
        if isinstance(ranges[0], pyomo.base.range.NumericRange):
            lb, ub = ranges[0].start, ranges[0].end
            if ranges[0].step < 0:
                lb, ub = ub, lb
            if (lb is not None) and (not ranges[0].closed[0]):
                lb = float(np.nextafter(lb, 1))
            if (ub is not None) and (not ranges[0].closed[1]):
                ub = float(np.nextafter(ub, -1))
            params[params_name] = p.Scalar(lower=lb, upper=ub)
            if ranges[0].step in [-1, 1]:
                # May consider using nested param
                params[params_name].set_integer_casting()  # type: ignore
        else:
            raise NotImplementedError(f"Cannot handle range type {type(ranges[0])}")
    elif isinstance(domain, pyomo.FiniteSimpleRangeSet):
        # Need to handle step size
        params[params_name] = p.Choice([range(*r) for r in domain.ranges()])  # Assume the ranges do not overlapped
    else:
        raise NotImplementedError(f"Cannot handle domain type {type(domain)}")
    return params


def _make_pyomo_variable_to_parametrization(model_component: pyomo.Var, params: ParamDict) -> ParamDict:
    # https://pyomo.readthedocs.io/en/stable/pyomo_modeling_components/Sets.html
    # Refer to the implementation in pyomo/core/base/var.py
    # To further improve the readability function, we should find out how to represent {None: ng.p.Scalar(), 1: ng.p.Scalar()} in ng.p.Dict
    # We do not adopt nested parameterization, which will require type information between string and int.
    # Such conversion has to be done in _pyomo_obj_function_wrapper and _pyomo_constraint_wrapper, which slows down optimization.
    if not isinstance(model_component, (pyomo.base.var.IndexedVar, pyomo.base.var.SimpleVar)):
        raise NotImplementedError  # Normally, Pyomo will create a set for the indices used by a variable
    for k, v in model_component._data.items():
        if isinstance(v, pyomo.base.var._GeneralVarData):
            if v.is_fixed():
                raise NotImplementedError
            if k is None:
                params_name = str(model_component.name)
            else:
                params_name = f"{model_component.name}[{_convert_to_ng_name(k)}]"
            if isinstance(v.domain, pyomo.RangeSet):
                params = _make_pyomo_range_set_to_parametrization(v.domain, params, params_name)
            elif isinstance(v.domain, pyomo.Set) and v.domain.isfinite():
                if v.domain.isordered():
                    params[params_name] = p.Choice(list(v.domain.ordered_data()))
                else:
                    params[params_name] = p.Choice(list(v.domain.data()))
            else:
                raise NotImplementedError(f"Cannot handle domain type {type(v.domain)}")
        else:
            raise NotImplementedError(f"Cannot handle variable type {type(v)}")
    return params


class Pyomo(base.ExperimentFunction):
    """Function calling Pyomo model

    Parameters
    ----------
    model: pyomo.environ.model
        Pyomo model

    Returns
    -------
    float
        the fitness

    Notes
    -----
    - You will require an Pyomo installation (with pip: "pip install pyomo")
    - Any changes on the model externally can lead to unexpected behaviours.
    """

    def __init__(self, model: pyomo.Model) -> None:
        if isinstance(model, pyomo.ConcreteModel):
            self._model_instance = model.clone()  # To enable the objective function to run in parallel
        else:
            raise NotImplementedError("AbstractModel is not supported. Please use create_instance() in Pyomo to create a model instance.")

        instru_params: ParamDict = {}
        self.all_vars: tp.List[pyomo.Var] = []
        self.all_params: tp.List[pyomo.Param] = []
        self.all_constraints: tp.List[pyomo.Constraint] = []
        self.all_objectives: tp.List[pyomo.Objective] = []

        # Relevant document: https://pyomo.readthedocs.io/en/stable/working_models.html

        for v in self._model_instance.component_objects(pyomo.Var, active=True):
            self.all_vars.append(v)
            _make_pyomo_variable_to_parametrization(v, instru_params)
        for v in self._model_instance.component_objects(pyomo.Param, active=True):
            self.all_params.append(v)
        for v in self._model_instance.component_objects(pyomo.Constraint, active=True):
            self.all_constraints.append(v)
        for v in self._model_instance.component_objects(pyomo.Objective, active=True):
            if v.sense == -1:
                print(f"Only minimization problem is supported. The value of the objective function {v.name} will be multiplied by -1.")
            self.all_objectives.append(v)

        if not self.all_objectives:
            raise NotImplementedError("Cannot find objective function")

        if len(self.all_objectives) > 1:
            raise NotImplementedError("Multi-objective function is not supported yet.")

        instru = p.Instrumentation(**instru_params)
        for c_idx in range(0, len(self.all_constraints)):
            instru.register_cheap_constraint(partial(self._pyomo_constraint_wrapper, c_idx))
        super().__init__(function=partial(self._pyomo_obj_function_wrapper, 0), parametrization=instru)  # Single objective

        exp_tag = ",".join([n.name for n in self.all_objectives])
        exp_tag += "|" + ",".join([n.name for n in self.all_vars])
        exp_tag += "|" + ",".join([n.name for n in self.all_constraints])
        self.register_initialization(name=exp_tag, model=self._model_instance)
        self._descriptors.update(name=exp_tag)

    def _pyomo_obj_function_wrapper(self, i: int, **k_model_variables: tp.Dict[str, tp.Any]) -> float:
        for k, v in k_model_variables.items():
            # TODO find a way to avoid exec
            exec(f"self._model_instance.{k} = {v}")  # pylint: disable=exec-used
        return float(pyomo.value(self.all_objectives[i] * self.all_objectives[i].sense))  # Single objective assumption

    def _pyomo_constraint_wrapper(self, i: int, instru: tp.ArgsKwargs) -> bool:
        k_model_variables = instru[1]
        # Combine all constraints into single one
        for k, v in k_model_variables.items():
            exec(f"self._model_instance.{k} = {v}")  # pylint: disable=exec-used
        if isinstance(self.all_constraints[i], pyomo.base.constraint.SimpleConstraint):
            return bool(pyomo.value(self.all_constraints[i].expr(self._model_instance)))
        elif isinstance(self.all_constraints[i], pyomo.base.constraint.IndexedConstraint):
            ret = True
            for k, c in self.all_constraints[i].items():
                ret = ret and pyomo.value(c.expr(self._model_instance))
                if not ret:
                    break
            return ret
        else:
            raise NotImplementedError(f"Constraint type {self.all_constraints[i].ctype} is not supported yet.")


# Simple Pyomo models, based on https://www.ima.umn.edu/materials/2017-2018.2/W8.21-25.17/26326/3_PyomoFundamentals.pdf.

# Rosenbrock
rosenbrock = pyomo.ConcreteModel() 
rosenbrock.x = pyomo.Var(initialize=-1.2, bounds=(-2, 2)) 
rosenbrock.y = pyomo.Var(initialize= 1.0, bounds=(-2, 2)) 
rosenbrock.obj = pyomo.Objective(expr=(1-rosenbrock.x)**2 + 100*(rosenbrock.y-rosenbrock.x**2)**2, sense=pyomo.minimize)


# Knapsack
items = ['hammer', 'wrench', 'screwdriver', 'towel'] 
v = {'hammer':8, 'wrench':3, 'screwdriver':6, 'towel':11} 
w = {'hammer':5, 'wrench':7, 'screwdriver':4, 'towel':3} 
W_max = 14 

knapsack = pyomo.ConcreteModel() 
knapsack.x = pyomo.Var(items, within=pyomo.Binary) 
knapsack.value = pyomo.Objective(expr=sum(v[i]*knapsack.x[i] for i in items), sense=pyomo.maximize) 
knapsack.weight = pyomo.Constraint(expr=sum(w[i]*knapsack.x[i] for i in items) <= W_max)


# P-median
N = 3 
M = 4 
P = 3 
d = {(1, 1): 1.7, (1, 2): 7.2, (1, 3): 9.0, (1, 4): 8.3, (2, 1): 2.9, (2, 2): 6.3, (2, 3): 9.8, (2, 4): 0.7, (3, 1): 4.5, (3, 2): 4.8, (3, 3): 4.2, (3, 4): 9.3} 
pmedian = pyomo.ConcreteModel() 
pmedian.Locations = range(N) 
pmedian.Customers = range(M) 
pmedian.x = pyomo.Var(pmedian.Locations, pmedian.Customers, bounds=(0.0,1.0)) 
pmedian.y = pyomo.Var(pmedian.Locations, within=pyomo.Binary)

pmedian.obj = pyomo.Objective(expr=sum(d[n,m]*model.x[n,m] for n in model.Locations for m in pmedian.Customers))
model.single_x = pyomo.ConstraintList()
for m in model.Customers:
    pmedian.single_x.add(sum(model.x[n,m] for n in model.Locations) == 1.0) 

pmedian.bound_y = pyomo.ConstraintList()
for n in model.Locations: 
    for m in model.Customers: 
        pmedian.bound_y.add(pmedian.x[n,m] <= pmedian.y[n] ) 
        pmedian.num_facilities = pyomo.Constraint(expr=sum(pmedian.y[n] for n in pmedian.Locations ) == P)


# Converting to Nevergrad.
pyomo_rosenbrok = Pyomo(rosenbrock)
pyomo_pmedian = Pyomo(pmedian)
pyomo_knapsack = Pyomo(knapsack)