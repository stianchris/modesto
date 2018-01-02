from __future__ import division

import logging
from math import pi, log, exp
import pandas as pd
from pyomo.core.base import Block, Param, Var, Constraint, NonNegativeReals
import numpy as np

from parameter import StateParameter, DesignParameter, UserDataParameter, DataFrameParameter


class Component(object):

    def __init__(self, name=None, horizon=None, time_step=None, params=None, direction=None, temperature_driven=False):
        """
        Base class for components

        :param name: Name of the component
        :param horizon: Horizon of the optimization problem, in seconds
        :param time_step: Time between two points
        :param params: Required parameters to set up the model (dict)
        :param direction: Indicates  direction of positive heat and mass flows. 1 means into the network (producer node), -1 means into the component (consumer node)
        """
        self.logger = logging.getLogger('modesto.component.Component')
        self.logger.info('Initializing Component {}'.format(name))

        self.name = name
        assert horizon % time_step == 0, "The horizon of the optimization problem should be multiple of the time step."
        self.horizon = horizon
        self.time_step = time_step
        self.n_steps = int(horizon / time_step)

        self.model = None  # The entire optimization model
        self.parent = None  # The node model
        self.block = None  # The component model

        self.params = params
        self.slack_list = []

        self.cp = 4180  # TODO make this static variable
        self.rho = 1000

        self.temperature_driven = temperature_driven

        if direction is None:
            raise ValueError('Set direction either to 1 or -1.')
        elif direction not in [-1, 1]:
            raise ValueError('Direction should be -1 or 1.')
        self.direction = direction

    def create_params(self):
        """
        Create all required parameters to set up the model

        :return: a dictionary, keys are the names of the parameters, values are the Parameter objects
        """
        return {}

    def pprint(self, txtfile=None):
        """
        Pretty print this block

        :param txtfile: textfile location to write to (default None => stdout)
        :return:
        """
        if self.block is not None:
            self.block.pprint(ostream=txtfile)
        else:
            Exception('The optimization model of %s has not been built yet.' % self.name)

    def get_params(self):
        """

        :return: A list of all parameters necessary for this type of component
        """

        return self.params.keys()

    def get_param_value(self, name, time=None):
        """
        Gets value of specified design param. Returns "None" if unknown

        :param name: Name of the parameter (str)
        :param time: If parameter consists of a series of values, the value at a certain can be selected time
        :return:
        """

        try:
            param = self.params[name]
        except KeyError:
            param = None
            self.logger.warning('Parameter {} does not (yet) exist in this component'.format(name))

        return param.get_value(time)

    def get_temperature(self, t, line):
        """
        Return temperature in one of both lines at time t

        :param t: time
        :param line: 'supply' or 'return'
        :return:
        """
        if not self.temperature_driven:
            raise ValueError('The model is not temperature driven, with no supply temperature variables')
        if self.block is None:
            raise Exception("The optimization model for %s has not been compiled" % self.name)
        if not line in self.model.lines:
            raise ValueError('The input line can only take the values from {}'.format(self.model.lines.value))

        return self.block.temperatures[t, line]

    def get_heat(self, t):
        """
        Return heat_flow variable at time t

        :param t:
        :return:
        """
        if self.block is None:
            raise Exception("The optimization model for %s has not been compiled" % self.name)
        return self.direction * self.block.heat_flow[t]

    def get_mflo(self, t, compiled=True):
        """
        Return mass_flow variable at time t

        :param t:
        :param compiled: If True, the compilation of the model is assumed to be finished. If False, other means to get to the mass flow are used
        :return:
        """
        # TODO Find something better!
        if not compiled:
            try:
                return self.direction * self.params['heat_profile'].v(t) * self.params['mult'].v()\
                         / self.cp / self.params['delta_T'].v()
            except:
                try:
                    return self.direction * self.params['heat_profile'].v() \
                           / self.cp / self.params['delta_T'].v()
                except:
                    return None
        else:
            if self.block is None:
                raise Exception("The optimization model for %s has not been compiled" % self.name)
            return self.direction * self.block.mass_flow[t]

    def get_direction(self):
        """
        Return direction

        :return:
        """
        return self.direction

    def get_slack(self, slack_name, t):
        """
        Get the value of a slack variable at a certian time

        :param slack_name: Name of the slack variable
        :param t: Time
        :return: Value of slack
        """

        return self.block.find_component(slack_name)[t]

    def make_block(self, parent):
        """
        Make a separate block in the parent model.
        This block is used to add the component model.

        :param parent: The node model to which it should be added
        :return:
        """

        self.parent = parent
        # If block is already present, remove it
        if self.parent.component(self.name) is not None:
            self.parent.del_component(self.name)
            # self.logger.warning('Overwriting block {} in Node {}'.format(self.name, self.parent.name))
            # TODO this test should be located in node; then no knowledge of parent would be needed
        self.parent.add_component(self.name, Block())  # TODO this too
        self.block = self.parent.__getattribute__(self.name)

        self.logger.info(
            'Optimization block for Component {} initialized'.format(self.name))

    def compile(self, topmodel, parent):
        """
        Add all necessary equations to the model

        :return:
        """
        self.check_data()

        self.model = topmodel
        self.make_block(parent)

        self.create_opt_params()

    def change_param(self, param, new_data):
        """
        Change the value of a parameter

        :param param: Name of the kind of user data
        :param new_data: The new value of the parameter
        :return:
        """
        if param not in self.params:
            raise Exception("{} is not recognized as a valid parameter for {}".format(param, self.name))

        self.params[param].change_value(new_data)

    def check_data(self):
        """
        Check if all data required to build the optimization problem is available

        :return:
        """
        for name, param in self.params.items():
            if not param.check():
                raise Exception('{} of {} does not have a value yet. Please, add one before optimizing.\n{}'. \
                                format(name, self.name, param.get_description()))

    def get_param_description(self, name):
        """
        Returns a string containing the description of a parameter

        :param name: Name of the parameter. If None, all parameters are returned
        :return: A dict of all descriptions
        """

        if name not in self.params:
                raise KeyError('{} is not an existing parameter for {}'.format(name, self.name))
        else:
            return self.params[name].get_description()

    def obj_slack(self):
        """
        Yields summation of all slack variables

        :return:
        """
        slack = 0

        for slack_name in self.slack_list:
            slack += sum(self.get_slack(slack_name, t) for t in self.model.TIME)

        return slack

    def obj_energy(self):
        """
        Yield summation of energy variables for objective function, but only for relevant component types

        :return:
        """
        return 0

    def obj_cost(self):
        """
        Yield summation of energy variables for objective function, but only for relevant component types

        :return:
        """
        return 0

    def obj_cost_ramp(self):
        """
        Yield summation of energy variables for objective function, but only for relevant component types

        :return:
        """
        return 0

    def obj_co2(self):
        """
        Yield summation of energy variables for objective function, but only for relevant component types

        :return:
        """
        return 0

    def obj_temp(self):
        """
        Yield summation of temperatures for objective function, but only for relevant component types

        :return:
        """
        return 0

    def create_opt_params(self):
        """
        Create the Pyomo Parameter objects for each of the parameters of the component

        :return:
        """

        for name, param in self.params.items():

            if isinstance(param, DataFrameParameter):
                def _par_decl_df(b, t):
                    return param.v(t)

                self.block.add_component(name, Param(param.get_index(), doc=name, rule=_par_decl_df, mutable=False))

            elif isinstance(param, StateParameter):
                # Prevent the state (as a function of time) is overwritten by parameter
                pass
            else:
                self.block.add_component(name, Param(doc=name, initialize=param.v(), mutable=True))

    def add_init_constraint(self, state_name, init_type, value=None):
        """
        Add an initialization constraint to the model

        :param state_name: Name of the state that is being initialized
        :param init_type: Type of initialization (free, cyclic or fixedVal)
        :param value: In case of a fixed value, this input indicates the initial value
        :return:
        """
        state = self.block.find_component(state_name)

        if init_type == 'free':
            pass
        elif init_type == 'cyclic':
            def _eq_init(b):
                return state[0] == state[self.model.TIME[-1]]

            self.block.add_component(state_name + '_init_eq', Constraint(rule=_eq_init))

        elif init_type == 'fixedVal':
            if value is None:
                ValueError('In case of initialization type \'fixedVal\', value cannot be None')
            def _eq_init(b):
                return state[0] == value

            self.block.add_component(state_name + '_init_eq', Constraint(rule=_eq_init))

    def add_all_init_constraints(self):
        """
        Add all initialization constraints to the problem

        :return:
        """
        for name, param in self.params.items():
            if isinstance(param, StateParameter):
                self.add_init_constraint(state_name=name,
                                         init_type=param.get_init_type(),
                                         value=param.v())

    def add_state_bounds_no_slack(self, state_name, ub=None, lb=None):
        # TODO only single numbers possible, add time series!

        state = self.block.find_component(state_name)

        if ub is not None:
            def _ub(b, t):
                return state[t] <= ub

            self.block.add_component(state_name + '_ub', Constraint(self.model.TIME, rule=_ub))

        if lb is not None:
            def _lb(b, t):
                return state[t] >= lb

            self.block.add_component(state_name + '_lb', Constraint(self.model.TIME, rule=_lb))

    def add_state_bounds_slack(self, state_name, ub=None, lb=None):
        # TODO only single numbers possible, add time series!

        u_slack_name = state_name + '_uslack'
        l_slack_name = state_name + '_lslack'

        self.slack_list.append(u_slack_name)
        self.slack_list.append(l_slack_name)

        self.block.add_component(u_slack_name, Var(self.model.TIME, within=NonNegativeReals))
        self.block.add_component(l_slack_name, Var(self.model.TIME, within=NonNegativeReals))

        state = self.block.find_component(state_name)
        u_slack = self.block.find_component(u_slack_name)
        l_slack = self.block.find_component(l_slack_name)

        if ub is not None:
            def _ub(b, t):
                return state[t] <= ub + u_slack[t]

            self.block.add_component(state_name + '_ub', Constraint(self.model.TIME, rule=_ub))

        if lb is not None:
            def _lb(b, t):
                return state[t] >= lb - l_slack[t]

            self.block.add_component(state_name + '_lb', Constraint(self.model.TIME, rule=_lb))

    def add_all_state_bounds(self):
        for param, param_obj in self.params.items():
            if isinstance(param_obj, StateParameter):
                ub = param_obj.get_upper_bound()
                lb = param_obj.get_lower_bound()

                if param_obj.get_slack():
                    self.add_state_bounds_slack(param, ub, lb)
                else:
                    self.add_state_bounds_no_slack(param, ub, lb)

class FixedProfile(Component):
    def __init__(self, name=None, horizon=None, time_step=None, direction=None, temperature_driven=False):
        """
        Class for a component with a fixed heating profile

        :param name: Name of the building
        :param horizon: Horizon of the optimization problem,
        in seconds
        :param time_step: Time between two points
        :param direction: Indicates  direction of positive heat and mass flows. 1 means into the network (producer node), -1 means into the component (consumer node)
        """
        super(FixedProfile, self).__init__(name=name,
                                           horizon=horizon,
                                           time_step=time_step,
                                           direction=direction,
                                           temperature_driven=temperature_driven)

        self.params = self.create_params()

    def create_params(self):
        """
        Creates all necessary parameters for the component

        :returns
        """

        params = {
            'delta_T': DesignParameter('delta_T',
                                       'Temperature difference across substation',
                                       'K'),
            'mult': DesignParameter('mult',
                                    'Number of buildings in the cluster',
                                    '-'),
            'heat_profile': UserDataParameter('heat_profile',
                                              'Heat use in one (average) building',
                                              'W'),
        }

        if self.temperature_driven:
            params['mass_flow'] = UserDataParameter('mass_flow',
                                                    'Mass flow through one (average) building substation',
                                                    'kg/s')
            params['temperature_supply'] = StateParameter('temperature_supply',
                                                          'Initial supply temperature at the component',
                                                          'K',
                                                          'fixedVal')
            params['temperature_return'] = StateParameter('temperature_return',
                                                          'Initial return temperature at the component',
                                                          'K',
                                                          'fixedVal')
            params['temperature_max'] = DesignParameter('temperature_max',
                                                          'Maximun allowed water temperature at the component',
                                                          'K')
            params['temperature_min'] = DesignParameter('temperature_min',
                                                          'Minimum allowed temperature at the component',
                                                          'K')

        return params

    def compile(self, topmodel, parent):
        """
        Build the structure of fixed profile

        :param topmodel: The main optimization model
        :param parent: The node model
        :return:
        """
        Component.compile(self, topmodel, parent)

        def _heat_flow(b, t):
            return b.mult * b.heat_profile[t]

        self.block.heat_flow = Param(self.model.TIME, rule=_heat_flow)

        if self.temperature_driven:
            self.block.temperatures = Var(self.model.TIME, self.model.lines,
                                          bounds=(self.params['temperature_min'].v(),
                                                  self.params['temperature_max'].v()))

            def _decl_temperatures(b, t):
                if t == 0:
                    return Constraint.Skip
                elif b.mass_flow[t] == 0:
                    return  b.temperatures[t, 'supply'] == b.temperatures[t, 'return']
                else:
                    return b.temperatures[t, 'supply'] - b.temperatures[t, 'return'] == \
                           b.heat_flow[t]/b.mass_flow[t]/self.cp

            def _init_temperatures(b, l):
                return b.temperatures[0, l] == self.params['temperature_' + l].v()

            self.block.decl_temperatures = Constraint(self.model.TIME, rule=_decl_temperatures)
            self.block.init_temperatures = Constraint(self.model.lines, rule=_init_temperatures)

        else:

            def _mass_flow(b, t):
                return b.mult * b.heat_profile[t] / self.cp / b.delta_T

            self.block.mass_flow = Param(self.model.TIME, rule=_mass_flow)

        self.add_all_state_bounds()

        self.logger.info('Optimization model {} {} compiled'.
                         format(self.__class__, self.name))

    # def fill_opt(self):
    #     """
    #     Add the parameters to the model
    #
    #     :return:
    #     """
    #
    #     param_list = ""
    #
    #     assert set(self.needed_design_param) >= set(self.design_param.keys()), \
    #         "Design parameters for %s are missing: %s" \
    #         % (self.name, str(list(set(self.design_param.keys()) - set(self.needed_design_param))))
    #
    #     assert set(self.needed_user_data) >= set(self.user_data.keys()), \
    #         "User data for %s are missing: %s" \
    #         % (self.name, str(list(set(self.user_data.keys()) - set(self.needed_user_data))))
    #
    #     for d_param in self.needed_design_param:
    #         param_list += "param %s := \n%s\n;\n" % (self.name + "_" + d_param, self.design_param[d_param])
    #
    #     for u_param in self.needed_user_data:
    #         param_list += "param %s := \n" % (self.name + "_" + u_param)
    #         for i in range(self.n_steps):
    #             param_list += str(i + 1) + ' ' + str(self.user_data[u_param].loc[i][0]) + "\n"
    #         param_list += ';\n'
    #
    #     return param_list


class VariableProfile(Component):
    # TODO Assuming that variable profile means State-Space model

    def __init__(self, name, horizon, time_step, direction, temperature_driven=False):
        """
        Class for components with a variable heating profile

        :param name: Name of the building
        :param horizon: Horizon of the optimization problem,
        in seconds
        :param time_step: Time between two points
        :param direction: Standard heat and mass flow direction for positive flows. 1 for producer components, -1 for consumer components
        """
        super(VariableProfile, self).__init__(name=name,
                                              horizon=horizon,
                                              time_step=time_step,
                                              direction=direction,
                                              temperature_driven=temperature_driven)

        self.params = self.create_params()


class BuildingFixed(FixedProfile):
    def __init__(self, name, horizon, time_step, temperature_driven=False):
        """
        Class for building models with a fixed heating profile

        :param name: Name of the building
        :param horizon: Horizon of the optimization problem,
        in seconds
        :param time_step: Time between two points
        """
        super(BuildingFixed, self).__init__(name=name,
                                            horizon=horizon,
                                            time_step=time_step,
                                            direction=-1,
                                            temperature_driven=temperature_driven)


class BuildingVariable(Component):
    # TODO How to implement DHW tank? Separate model from Building or together?
    # TODO Model DHW user without tank? -> set V_tank = 0

    def __init__(self, name, horizon, time_step, temperature_driven=False):
        """
        Class for a building with a variable heating profile

        :param name: Name of the building
        :param horizon: Horizon of the optimization problem,
        in seconds
        :param time_step: Time between two points
        """
        super(BuildingVariable, self).__init__(name=name,
                                               horizon=horizon,
                                               time_step=time_step,
                                               direction=-1,
                                               temperature_driven=temperature_driven)


class ProducerFixed(FixedProfile):
    def __init__(self, name, horizon, time_step, temperature_driven=False):
        """
        Class that describes a fixed producer profile

        :param name: Name of the building
        :param horizon: Horizon of the optimization problem,
        in seconds
        :param time_step: Time between two points
        """
        super(ProducerFixed, self).__init__(name=name,
                                            horizon=horizon,
                                            time_step=time_step,
                                            direction=1,
                                            temperature_driven=temperature_driven)


class ProducerVariable(Component):
    def __init__(self, name, horizon, time_step, temperature_driven=False):
        """
        Class that describes a variable producer

        :param name: Name of the building
        :param horizon: Horizon of the optimization problem,
        in seconds
        :param time_step: Time between two points
        """

        super(ProducerVariable, self).__init__(name=name,
                                               horizon=horizon,
                                               time_step=time_step,
                                               direction=1,
                                               temperature_driven=temperature_driven)

        self.params = self.create_params()

        self.logger = logging.getLogger('modesto.components.VarProducer')
        self.logger.info('Initializing VarProducer {}'.format(name))

    def create_params(self):
        params = {
            'efficiency': DesignParameter('efficiency',
                                          'Efficiency of the heat source',
                                          '-'),
            'PEF': DesignParameter('PEF',
                                   'Factor to convert heat source to primary energy',
                                   '-'),
            'CO2': DesignParameter('CO2',
                                   'amount of CO2 released when using primary energy source',
                                   'kg/kWh'),
            'fuel_cost': UserDataParameter('fuel_cost',
                                           'cost of fuel/electricity to generate heat',
                                           'euro/MWh'),
            'Qmax': DesignParameter('Qmax',
                                    'Maximum possible heat output',
                                    'W'),
            'ramp': DesignParameter('ramp',
                                    'Maximum ramp (increase in heat output)',
                                    'W/s'),
            'ramp_cost': DesignParameter('ramp_cost',
                                         'Ramping cost',
                                         'euro/(W/s)')
        }

        if self.temperature_driven:
            params['mass_flow'] = UserDataParameter('mass_flow',
                                                    'Flow through the production unit substation',
                                                    'kg/s')
            params['temperature_max'] = DesignParameter('temperature_max',
                                                        'Maximum allowed water temperature',
                                                        'K')
            params['temperature_min'] = DesignParameter('temperature_min',
                                                        'Minimum allowed water temperature',
                                                        'K')
            params['temperature_supply'] = StateParameter('temperature_supply',
                                                          'Initial supply temperature at the component',
                                                          'K',
                                                          'fixedVal')
            params['temperature_return'] = StateParameter('temperature_return',
                                                          'Initial return temperature at the component',
                                                          'K',
                                                          'fixedVal')
        return params

    def compile(self, topmodel, parent):
        """
        Build the structure of a producer model

        :return:
        """
        Component.compile(self, topmodel, parent)

        self.block.heat_flow = Var(self.model.TIME, bounds=(0, self.params['Qmax'].v()))
        self.block.ramping_cost = Var(self.model.TIME)

        if self.temperature_driven:

            def _decl_init_heat_flow(b):
                return b.heat_flow[0] == (self.params['temperature_supply'].v() -
                                          self.params['temperature_return'].v()) * \
                                         self.cp * b.mass_flow[0]

            self.block.decl_init_heat_flow = Constraint(rule=_decl_init_heat_flow)

        else:
            self.block.mass_flow = Var(self.model.TIME, within=NonNegativeReals)

        def _decl_upward_ramp(b, t):
            if t == 0:
                return Constraint.Skip
            else:
                return b.heat_flow[t] - b.heat_flow[t-1] <= self.params['ramp'].v()*self.time_step

        def _decl_downward_ramp(b, t):
            if t == 0:
                return Constraint.Skip
            else:
                return b.heat_flow[t-1] - b.heat_flow[t] <= self.params['ramp'].v() * self.time_step

        def _decl_upward_ramp_cost(b, t):
            if t == 0:
                return b.ramping_cost[t] == 0
            else:
                return b.ramping_cost[t] >= (b.heat_flow[t] - b.heat_flow[t-1])*self.params['ramp_cost'].v()

        def _decl_downward_ramp_cost(b, t):
            if t == 0:
                return Constraint.Skip
            else:
                return b.ramping_cost[t] >= (b.heat_flow[t-1] - b.heat_flow[t])*self.params['ramp_cost'].v()

        self.block.decl_upward_ramp = Constraint(self.model.TIME, rule=_decl_upward_ramp)
        self.block.decl_downward_ramp = Constraint(self.model.TIME, rule=_decl_downward_ramp)
        self.block.decl_downward_ramp_cost = Constraint(self.model.TIME, rule=_decl_downward_ramp_cost)
        self.block.decl_upward_ramp_cost = Constraint(self.model.TIME, rule=_decl_upward_ramp_cost)

        if self.temperature_driven:

            self.block.temperatures = Var(self.model.TIME,
                                          self.model.lines)

            def _limit_temperatures(b, t):
                return self.params['temperature_min'].v() <= b.temperatures[t, 'supply'] <= self.params['temperature_max'].v()

            self.block.limit_teperatures = Constraint(self.model.TIME, rule=_limit_temperatures)

            def _decl_temperatures(b, t):
                if t == 0:
                    return Constraint.Skip
                elif b.mass_flow[t] == 0:
                    return Constraint.Skip
                else:
                    return b.temperatures[t, 'supply'] - b.temperatures[t, 'return'] == b.heat_flow[t]/b.mass_flow[t]/self.cp

            def _init_temperature(b, l):
                return b.temperatures[0, l] == self.params['temperature_' + l].v()

            def _decl_temp_mf0(b, t):
                if (not t == 0) and b.mass_flow[t] == 0:
                    return b.temperatures[t, 'supply'] == b.temperatures[t-1, 'supply']
                else:
                    return Constraint.Skip

            self.block.decl_temperatures = Constraint(self.model.TIME, rule=_decl_temperatures)
            self.block.init_temperatures = Constraint(self.model.lines, rule=_init_temperature)
            self.block.dec_temp_mf0 = Constraint(self.model.TIME, rule=_decl_temp_mf0)

        self.add_all_state_bounds()

    def get_ramp_cost(self, t):
        return self.block.ramping_cost[t]

    # TODO Objectives are all the same, only difference is the value of the weight...

    def obj_energy(self):
        """
        Generator for energy objective variables to be summed
        Unit: kWh (primary energy)

        :return:
        """

        return sum(self.block.PEF / self.block.efficiency * self.get_heat(t) * self.time_step / 3600 for t in range(self.n_steps))

    def obj_cost(self):
        """
        Generator for cost objective variables to be summed
        Unit: euro

        :return:
        """
        return sum(self.block.fuel_cost[t] * self.get_heat(t) / self.block.efficiency *
                   self.time_step / 3600 / 10**6 for t in range(self.n_steps))

    def obj_cost_ramp(self):
        """
        Generator for cost objective variables to be summed
        Unit: euro

        :return:
        """
        return sum(self.get_ramp_cost(t) + self.block.fuel_cost[t]
                    * self.get_heat(t) / self.block.efficiency *
                   self.time_step / 3600 / 10**6 for t in range(self.n_steps)) #

    def obj_co2(self):
        """
        Generator for CO2 objective variables to be summed
        Unit: kg CO2

        :return:
        """

        return sum(self.block.CO2 / self.block.efficiency * self.get_heat(t) * self.time_step / 3600 for t in range(self.n_steps))

    def obj_temp(self):
        """
        Generator for supply and return temperatures to be summed
        Unit: K

        :return:
        """

        # return sum((70+273.15 - self.get_temperature(t, 'supply'))**2 for t in range(self.n_steps))

        return sum(self.get_temperature(t, 'supply') for t in self.model.TIME)


class StorageFixed(FixedProfile):
    def __init__(self, name, horizon, time_step, temperature_driven):
        """
        Class that describes a fixed storage

        :param name: Name of the building
        :param horizon: Horizon of the optimization problem,
        in seconds
        :param time_step: Time between two points
        """
        super(StorageFixed, self).__init__(name=name,
                                           horizon=horizon,
                                           time_step=time_step,
                                           direction=-1,
                                           temperature_driven=temperature_driven)


class StorageVariable(Component):
    def __init__(self, name, horizon, time_step, temperature_driven=False):
        """
        Class that describes a variable storage

        :param name: Name of the building
        :param horizon: Horizon of the optimization problem,
        in seconds
        :param time_step: Time between two points
        """

        super(StorageVariable, self).__init__(name=name,
                                              horizon=horizon,
                                              time_step=time_step,
                                              direction=-1,
                                              temperature_driven=temperature_driven)

        self.params = self.create_params()

        # TODO choose between stored heat or state of charge as state (which one is easier for initialization?)

    def create_params(self):
        params = {
            'Thi': DesignParameter('Thi',
                                   'High temperature in tank',
                                   'K'),
            'Tlo': DesignParameter('Tlo',
                                   'Low temperature in tank',
                                   'K',),
            'mflo_max': DesignParameter('mflo_max',
                                        'Maximal mass flow rate to and from storage vessel',
                                        'kg/s'),
            'volume': DesignParameter('volume',
                                      'Storage volume',
                                      'm3'),
            'ar': DesignParameter('ar',
                                  'Aspect ratio (height/width)',
                                  '-'),
            'dIns': DesignParameter('dIns',
                                    'Insulation thickness',
                                    'm'),
            'kIns': DesignParameter('kIns',
                                    'Thermal conductivity of insulation material',
                                    'W/(m.K)'),
            'heat_stor': StateParameter('heat_stor',
                                        'Heat stored in the thermal storage unit',
                                        'J',
                                        'fixedVal')
        }

        return params

    def compile(self, topmodel, parent):
        """
        Compile this model

        :param topmodel: top optimization model with TIME and Te variable
        :param parent: block above this level
        :return:
        """

        # TODO Find easier way to define extra parameters here
        self.max_mflo = self.params['mflo_max'].v()
        self.volume = self.params['volume'].v()
        self.dIns = self.params['dIns'].v()
        self.kIns = self.params['kIns'].v()

        self.ar = self.params['ar'].v()

        self.temp_diff = self.params['Thi'].v() - self.params['Tlo'].v()
        self.temp_sup = self.params['Thi'].v()
        self.temp_ret = self.params['Tlo'].v()

        # Geometrical calculations
        w = (4 * self.volume / self.ar / pi) ** (1 / 3)  # Width of tank
        h = self.ar * w  # Height of tank

        Atb = w ** 2 / 4 * pi  # Top/bottom surface of tank

        # Heat transfer coefficients
        self.UAw = 2 * pi * self.kIns * h / log((w + 2 * self.dIns) / w)
        self.UAtb = Atb * self.kIns / self.dIns

        # Time constant
        self.tau = self.volume * 1000 * self.cp / self.UAw

        ############################################################################################
        # Initialize block

        self.model = topmodel
        self.make_block(parent)

        ############################################################################################
        # Parameters

        # Fixed heat loss
        def _heat_loss_ct(b, t):
            return self.UAw * (self.temp_ret - self.model.Te[t]) + \
                   self.UAtb * (self.temp_ret + self.temp_sup - self.model.Te[t])
        # TODO implement varying outdoor temperature

        self.block.heat_loss_ct = Param(self.model.TIME, rule=_heat_loss_ct)

        ############################################################################################
        # Initialize variables
        #       with upper and lower bounds

        mflo_bounds = (
            -self.max_mflo, self.max_mflo) if self.max_mflo is not None else (
            None, None)
        heat_bounds = (
            (-self.max_mflo * self.temp_diff * self.cp,
             self.max_mflo * self.temp_diff * self.cp) if self.max_mflo is not None else (
                None, None))

        # In/out
        self.block.mass_flow = Var(self.model.TIME, bounds=mflo_bounds)
        self.block.heat_flow = Var(self.model.TIME, bounds=heat_bounds)
        if self.temperature_driven:
            self.block.supply_temperature = Var(self.model.TIME)

        # Internal
        self.block.heat_stor = Var(self.model.TIME)

        max_energy = self.volume * self.cp * 1000 * self.temp_diff

        if (self.params['heat_stor'].ub > max_energy) or (self.params['heat_stor'].ub is None):
            self.params['heat_stor'].change_upper_bound(max_energy)
        self.logger.debug('Max heat: {}J'.format(str(self.volume * self.cp * 1000 * self.temp_diff)))
        self.logger.debug('Tau:      {}s'.format(str(self.tau)))
        self.logger.debug('Loss  :   {}%'.format(str(exp(-self.time_step / self.tau))))

        #############################################################################################
        # Equality constraints

        self.block.heat_loss = Var(self.model.TIME)

        def _eq_heat_loss(b, t):
            return b.heat_loss[t] == (1 - exp(-self.time_step / self.tau)) * b.heat_stor[t] / self.time_step + \
                                     b.heat_loss_ct[t]

        self.block.eq_heat_loss = Constraint(self.model.TIME, rule=_eq_heat_loss)

        # State equation
        def _state_eq(b, t):
            return b.heat_stor[t + 1] == b.heat_stor[t] + self.time_step * (b.heat_flow[t] - b.heat_loss[t])

            # self.tau * (1 - exp(-self.time_step / self.tau)) * (b.heat_flow[t] -b.heat_loss_ct[t])

        self.block.state_eq = Constraint(self.model.TIME, rule=_state_eq)

        #############################################################################################
        # Initial state

        self.add_all_init_constraints()

        #############################################################################################

        # Add state bounds

        self.add_all_state_bounds()

        #############################################################################################
        # Mass flow and heat flow link
        def _heat_bal(b, t):
            return self.cp * b.mass_flow[t] * self.temp_diff == b.heat_flow[t]

        self.block.heat_bal = Constraint(self.model.TIME, rule=_heat_bal)

        self.logger.info('Optimization model Storage {} compiled'.format(self.name))
