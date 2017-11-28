from __future__ import division

import sys
from math import sqrt

# noinspection PyUnresolvedReferences
import pyomo.environ
from component import *
from pipe import *
from parameter import *
from pyomo.core.base import ConcreteModel, Objective, minimize, value
from pyomo.core.base.param import IndexedParam
from pyomo.core.base.var import IndexedVar
from pyomo.opt import SolverFactory
from pyomo.opt import SolverStatus, TerminationCondition
import networkx as nx
import collections
import pandas as pd


class Modesto:
    def __init__(self, horizon, time_step, pipe_model, graph):
        """
        This class allows setting up optimization problems for district energy systems

        :param horizon: The horizon of the optimization problem, in seconds
        :param time_step: The time step between two points
        :param objective: String describing the objective of the optimization problem
        :param pipe_model: String describing the type of model to be used for the pipes
        :param graph: networkx object, describing the structure of the network
        """

        self.model = ConcreteModel()

        self.horizon = horizon
        self.time_step = time_step
        assert (horizon % time_step) == 0, "The horizon should be a multiple of the time step."
        self.n_steps = int(horizon / time_step)
        self.time = range(self.n_steps)

        self.pipe_model = pipe_model
        if pipe_model == 'NodeMethod':
            self.temperature_driven = True
        else:
            self.temperature_driven = False

        self.graph = graph
        self.results = None

        self.nodes = {}
        self.edges = {}
        self.components = {}

        self.params = self.create_params()

        self.logger = logging.getLogger('modesto.main.Modesto')

        self.allow_flow_reversal = True

        self.build(graph)
        self.compiled = False

        self.objectives = {}
        self.act_objective = None

    @staticmethod
    def create_params():

        params = {
            'Te': WeatherDataParameter('Te',
                                       'Ambient temperature',
                                       'K')
        }

        return params

    def build(self, graph):
        """
        Build the structure of the optimization problem
        Sets up the equations without parameters

        :param graph: Object containing structure of the network, structure and parameters describing component models and design parameters
        :return:
        """
        self.results = None

        self.graph = graph

        self.__build_nodes()
        self.__build_edges()

    def __build_nodes(self):
        """
        Build the nodes of the network, adding components
        and their models

        :return:
        """
        self.nodes = {}
        self.components = {}

        for node in self.graph.nodes:
            # Create the node
            assert node not in self.nodes, "Node %s already exists" % node.name
            self.nodes[node] = (Node(node,
                                     self.graph,
                                     self.graph.nodes[node],
                                     self.horizon,
                                     self.time_step,
                                     self.temperature_driven))

            # Add the new components
            new_components = self.nodes[node].get_components()
            assert list(set(self.components.keys()).intersection(new_components.keys())) == [], \
                "Component(s) with name(s) %s is not unique!" \
                % str(list(set(self.components).intersection(new_components)))
            self.components.update(new_components)

    def __build_edges(self):
        """
        Build the branches (i.e. pips/connections between nodes)
        adding their models

        :return:
        """

        self.edges = {}

        for edge_tuple in self.graph.edges:
            edge = self.graph[edge_tuple[0]][edge_tuple[1]]
            start_node = self.nodes[edge_tuple[0]]
            end_node = self.nodes[edge_tuple[1]]

            assert edge['name'] not in self.edges, "An edge with name %s already exists" % edge['name']
            assert edge['name'] not in self.components, "A component with name %s already exists" % edge['name']

            # Create the modesto.Edge object
            self.edges[edge['name']] = Edge(name=edge['name'],
                                            edge=edge,
                                            start_node=start_node,
                                            end_node=end_node,
                                            horizon=self.horizon,
                                            time_step=self.time_step,
                                            pipe_model=self.pipe_model,
                                            allow_flow_reversal=self.allow_flow_reversal,
                                            temperature_driven=self.temperature_driven)
            # Add the modesto.Edge object to the graph
            self.graph[edge_tuple[0]][edge_tuple[1]]['conn'] = self.edges[edge['name']]
            self.components[edge['name']] = self.edges[edge['name']].pipe

    def change_graph(self):
        # TODO write this
        pass

    def compile(self):
        """
        Compile the optimization problem

        :return:
        """

        # Check if not compiled already
        if self.compiled:
            self.logger.warning('Model was already compiled.')

        # Check whether all necessary parameters are there
        self.check_data()

        # General parameters
        self.model.TIME = Set(initialize=self.time, ordered=True)
        self.model.lines = Set(initialize=['supply', 'return'])

        def _ambient_temp(b, t):
            return self.params['Te'].v(t)

        self.model.Te = Param(self.model.TIME, rule=_ambient_temp)

        # Components
        for name, edge in self.edges.items():
            edge.compile(self.model)
        for name, node in self.nodes.items():
            node.compile(self.model)

        self.build_objectives()

        self.compiled = True  # Change compilation flag

    def build_objectives(self):
        """
        Initialize different objectives

        :return:
        """

        def obj_energy(model):
            return sum(comp.obj_energy() for comp in self.iter_components())

        def obj_cost(model):
            return sum(comp.obj_cost() for comp in self.iter_components())

        def obj_co2(model):
            return sum(comp.obj_co2() for comp in self.iter_components())

        def obj_temp(model):
            return sum(comp.obj_co2() for comp in self.iter_components())

        self.model.OBJ_ENERGY = Objective(rule=obj_energy, sense=minimize)
        self.model.OBJ_COST = Objective(rule=obj_cost, sense=minimize)
        self.model.OBJ_CO2 = Objective(rule=obj_co2, sense=minimize)
        self.model.OBJ_TEMP = Objective(rule=obj_temp, sense=minimize)

        self.objectives = {
            'energy': self.model.OBJ_ENERGY,
            'cost': self.model.OBJ_COST,
            'co2': self.model.OBJ_CO2,
            'temp': self.model.OBJ_TEMP
        }

    def check_data(self):
        """
        Checks whether all parameters have been assigned a value,
        if not an error is raised

        """
        if self.temperature_driven:
            self.add_mf()

        for name, param in self.params.items():
            param.check()

        for comp in self.components:
            self.components[comp].check_data()

    def set_objective(self, objtype):
        """
        Set optimization objective.

        :param objtype:
        :return:
        """
        if objtype not in self.objectives:
            raise ValueError('Choose an objective type from {}'.format(objtypes))

        for obj in self.objectives.values():
            obj.deactivate()

        self.objectives[objtype].activate()
        self.act_objective = self.objectives[objtype]

        self.logger.debug('{} objective set'.format(objtype))

    def iter_components(self):
        """
        Function that generates a list of all components in all nodes of model

        :return: Component object list
        """
        return [self.components[comp] for comp in self.components]

    def solve(self, tee=False, mipgap=0.1, verbose=False):
        """
        Solve a new optimization

        :param tee: If True, print the optimization model
        :param mipgap: Set mip optimality gap. Default 10%
        :param verbose: True to print extra diagnostic information
        :return:
        """

        if tee:
            self.model.pprint()

        opt = SolverFactory("gurobi")
        # opt.options["Threads"] = threads
        opt.options["MIPGap"] = mipgap
        self.results = opt.solve(self.model, tee=tee)

        if verbose:
            print self.results

        if (self.results.solver.status == SolverStatus.ok) and (
                    self.results.solver.termination_condition == TerminationCondition.optimal):
            status = 0
        elif self.results.solver.termination_condition == TerminationCondition.infeasible:
            status = 1
            self.logger.warning('Model is infeasible')
        else:
            status = -1
            self.logger.error('Solver status: ', self.results.solver.status)

        return status

    def opt_settings(self, objective=None, horizon=None, time_step=None, pipe_model=None, allow_flow_reversal=None):
        """
        Change the setting of the optimization problem

        :param objective: Name of the optimization objective
        :param horizon: The horizon of the problem, in seconds
        :param time_step: The time between two points, in secinds
        :param pipe_model: The name of the type of pipe model to be used
        :param allow_flow_reversal: Boolean indicating whether mass flow reversals are possible in the pipes
        :return:
        """
        if objective is not None:  # TODO Do we need this to be defined at the top level of modesto?
            self.set_objective(objective)
        if horizon is not None:
            self.horizon = horizon
        if time_step is not None:
            self.time_step = time_step
        if pipe_model is not None:
            self.pipe_model = pipe_model
        if allow_flow_reversal is not None:
            self.allow_flow_reversal = allow_flow_reversal

    def change_general_param(self, param, val):
        """
        Change a parameter that can be used by all components

        :param param: Name of the parameter
        :param val: The new data
        :return:
        """
        assert param in self.params, '%s is not recognized as a valid parameter' % param
        self.params[param].change_value(val)

    def change_param(self, comp, param, val):
        """
        Change a parameter
        :param comp: Name of the component
        :param param: name of the parameter
        :param val: New value of the parameter
        :return:
        """
        assert comp in self.components, "%s is not recognized as a valid component" % comp
        self.components[comp].change_param(param, val)

    def change_state_bounds(self, comp, state, new_ub, new_lb, slack):
        """
        Change the interval of possible values of a certain state, and
        indicate whether it is a slack variable or not

        :param comp: Name of the component
        :param state: Name of the param
        :param new_ub: New upper bound
        :param new_lb:  New lower bound
        :param slack: Boolean indicating whether a slack should be added (True) or not (False)
        """
        # TODO Adapt method so you can change only one of the settings?
        if comp not in self.components:
            raise IndexError("%s is not recognized as a valid component" % comp)
        if state not in self.components[comp].params:
            raise IndexError('%s is not recognized as a valid parameter' % state)

        self.components[comp].params[state].change_upper_bound(new_ub)
        self.components[comp].params[state].change_lower_bound(new_lb)
        self.components[comp].params[state].change_slack(slack)

    def change_init_type(self, comp, state, new_type):
        """
        Change the type of initialization constraint

        :param comp: Name of the component
        :param state: Name of the state
        """
        if comp not in self.components:
            raise IndexError("%s is not recognized as a valid component" % comp)
        if state not in self.components[comp].params:
            raise IndexError('%s is not recognized as a valid parameter' % state)

        self.components[comp].params[state].change_init_type(new_type)

    def get_result(self, comp, name, index=None):
        """
        Returns the numerical values of a certain parameter or time-dependent variable after optimization

        :param comp: Name of the component to which the variable belongs
        :param name: Name of the needed variable/parameter
        :return: A list containing all values of the variable/parameter over the time horizon
        """

        if self.results is None:
            raise Exception('The optimization problem has not been solved yet.')
        if comp not in self.components:
            raise Exception('%s is not a valid component name' % comp)

        result = []

        obj = self.components[comp].block.find_component(name)
        if obj is None:
            raise Exception('{} is not a valid parameter or variable of {}'.format(name, comp))

        if isinstance(obj, IndexedVar):
            if index is None:
                for i in self.model.TIME:
                    result.append(obj.values()[i].value)

            else:
                for i in self.model.TIME:
                    result.append(obj[(i, index)].value)

            return result

        elif isinstance(obj, IndexedParam):
            result = obj.values()

            return result

        else:
            self.logger.warning('{}.{} was a different type of variable/parameter than what has been implemented: '
                                '{}'.format(comp, name, type(obj)))
            return None

    def get_objective(self, objtype=None):
        """
        Return value of objective function. With no argument supplied, the active objective is returned. Otherwise, the
        objective specified in the argument is returned.

        :param objtype: Name of the objective to be returned. Default None: returns the active objective.
        :return:
        """
        if objtype is None:
            # Find active objective
            if self.act_objective is not None:
                obj = self.act_objective
            else:
                raise ValueError('No active objective found.')

        else:
            assert objtype in self.objectives.keys(), 'Requested objective does not exist. Please choose from {}'.format(
                self.objectives.keys())
            obj = self.objectives[objtype]

        return value(obj)

    def print_all_params(self):
        """
        Print all parameters in the optimization problem

        :return:
        """
        descriptions = {'general': {}}
        for name, param in self.params.items():
            descriptions['general'][name] = param.get_description()

        for comp in self.components:
            descriptions[comp] = {}
            for name in self.components[comp].get_params():
                descriptions[comp][name] = self.components[comp].get_param_description(name)

        self._print_params(descriptions)

    def print_comp_param(self, comp, *args):
        """
        Print parameters of a component

        :param comp: Name of the component
        :param args: Names of the parameters, if None are given, all will be printed
        :return:
        """
        descriptions = {comp: {}}

        if comp not in self.components:
            raise IndexError('%s is not recognized a valid component' % comp)
        if not args:
            for name in self.components[comp].get_params():
                descriptions[comp][name] = self.components[comp].get_param_description(name)
        for name in args:
            if name not in self.components[comp].params:
                raise IndexError('%s is not a valid parameter of %s' % (name, comp))
            descriptions[comp][name] = self.components[comp].get_param_description(name)

        self._print_params(descriptions)

    def print_general_param(self, name):
        """
        Print a single, general parameter

        :param name: Name of the parameter
        :return:
        """

        if name not in self.params:
            raise IndexError('%s is not a valid general parameter ' % name)

        self._print_params({'general': {name: self.params[name].get_description()}})

    @staticmethod
    def _print_params(descriptions):
        for comp in descriptions:
            print '--- ', comp, ' ---\n'
            for param, des in descriptions[comp].items():
                print '-', param, '\n', des, '\n'

    def calculate_mf(self):
        """
        Given the heat demands of all substations, calculate the mass flow throughout the netire network

        :param producer_node: Name of the node for which the equation is kipped to get a determined system
        :return:
        """
        result = collections.defaultdict(list)

        # Take into account mult factors!!
        inc_matrix = -nx.incidence_matrix(self.graph, oriented=True).todense()

        nodes = self.get_nodes()
        edges = self.get_edges()

        # Remove one node and the corresponding row from the matrix to make the system determined
        left_out_node = nodes[-1]
        row_nr = nodes.index(left_out_node)
        row = inc_matrix[row_nr, :]
        nodes.remove(left_out_node)
        matrix = np.delete(inc_matrix, row_nr, 0)

        for t in self.time:
            mf_nodes = []

            for node in nodes:
                for comp in self.nodes[node].comps:
                    result[comp].append(self.components[comp].get_mflo(t, compiled=False))
                mf_node = self.nodes[node].get_mflo(t)
                result[node].append(mf_node)
                mf_nodes.append(mf_node)

            sol = np.linalg.solve(matrix, mf_nodes)

            for i, edge in enumerate(edges):
                result[edge].append(sol[i])

            result[left_out_node].append(sum(result[edge][-1] * row[0, i] for i, edge in enumerate(edges)))

            for comp in self.nodes[left_out_node].comps:
                result[comp].append(result[left_out_node][-1])

            # TODO Only one component at producer node possible at the moment

        return result

    def add_mf(self):
        mf = self.calculate_mf()
        mf_df = pd.DataFrame.from_dict(mf)

        for comp in self.components:
            self.change_param(comp, 'mass_flow', mf_df.loc[:, [comp]])

    def get_nodes(self):
        """
        Returns a list with the names of nodes (ordered in the same way as in the graph)

        :return:
        """

        return list(self.graph.nodes)

    def get_edges(self):
        """
        Returns a list with the names of edges (ordered in the same way as in the graph)

        :return:
        """
        tuples = list(self.graph.edges)
        dict = nx.get_edge_attributes(self.graph, 'name')
        edges = []
        for tuple in tuples:
            edges.append(dict[tuple])
        return edges


class Node(object):
    def __init__(self, name, graph, node, horizon, time_step, temperature_driven=False):
        """
        Class that represents a geographical network location,
        associated with a number of components and connected to other nodes through edges

        :param name: Unique identifier of node (str)
        :param graph: Networkx Graph object
        :param node: Networkx Node object
        :param horizon: Horizon of the problem
        :param time_step: Time step between two points of the problem
        """
        self.horizon = horizon
        self.time_step = time_step

        self.logger = logging.getLogger('modesto.Node')
        self.logger.info('Initializing Node {}'.format(name))

        self.name = name
        self.graph = graph
        self.node = node
        self.loc = self.get_loc

        self.model = None
        self.block = None
        self.comps = {}

        self.temperature_driven = temperature_driven

        self.build()

    def __get_data_point(self, name):
        assert name in self.node, "%s is not stored in the networkx node object for %s" % (name, self.name)
        return self.node[name]

    def get_loc(self):
        x = self.__get_data_point('x')
        y = self.__get_data_point('y')
        z = self.__get_data_point('z')
        return {'x': x, 'y': y, 'z': z}

    def get_components(self):
        """
        Collects the components and their type belonging to this node

        :return: A dict, with keys the names of the components, values the Component objects
        """
        return self.comps

    def add_comp(self, name, ctype):
        """
        Add component to Node. No component with the same name may exist in this node.

        :param name: name of the component
        :param ctype: type of the component
        :return:
        """

        assert name not in self.comps, 'Name must be a unique identifier for this node'

        def str_to_class(str):
            return reduce(getattr, str.split("."), sys.modules[__name__])

        try:
            cls = str_to_class(ctype)
        except AttributeError:
            cls = None

        if cls:
            obj = cls(name, horizon=self.horizon,
                      time_step=self.time_step,
                      temperature_driven=self.temperature_driven)
        else:
            raise ValueError("%s is not a valid class name! (component is %s, in node %s)" % (ctype, name, self.name))

        self.logger.info('Component {} added to {}'.format(name, self.name))

        return obj

    def build(self):
        """
        Compile this model and all of its submodels

        :param model: top level model
        :return: A list of the names of components that have been added
        """
        for component, type in self.__get_data_point("comps").items():
            self.comps[component] = self.add_comp(component, type)

        self.logger.info('Build of {} finished'.format(self.name))

    def compile(self, model):
        self._make_block(model)

        for name, comp in self.comps.items():
            comp.compile(model, self.block)

        self._add_bal()

        self.logger.info('Compilation of {} finished'.format(self.name))

    def _add_bal(self):
        """
        Add balance equations after all blocks for this node and subcomponents have been compiled

        :return:
        """

        pipes = self.get_edges()

        # TODO No mass flow reversal yet
        if self.temperature_driven:

            incoming_comps = collections.defaultdict(list)
            incoming_pipes = collections.defaultdict(list)
            outgoing_comps = collections.defaultdict(list)
            outgoing_pipes = collections.defaultdict(list)

            for name, comp in self.comps.items():
                if comp.get_direction() == 1:
                    incoming_comps['supply'].append(name)
                    outgoing_comps['return'].append(name)
                else:
                    outgoing_comps['supply'].append(name)
                    incoming_comps['return'].append(name)

            for name, pipe in pipes.items():
                if pipe.get_direction(self.name) == -1:
                    incoming_pipes['supply'].append(name)
                    outgoing_pipes['return'].append(name)
                else:
                    outgoing_pipes['supply'].append(name)
                    incoming_pipes['return'].append(name)

            self.block.mix_temp = Var(self.model.TIME, self.model.lines)

            c = self.comps
            p = pipes

            def _temp_bal_incoming(b, t, l):
                return (sum(c[comp].get_mflo(t) for comp in incoming_comps[l]) +
                       sum(p[pipe].get_mflo(self.name, t) for pipe in incoming_pipes[l])) * b.mix_temp[t, l] == \
                       sum(c[comp].get_mflo(t) * c[comp].get_temperature(t, l) for comp in incoming_comps[l]) + \
                       sum(p[pipe].get_mflo(self.name, t) * p[pipe].get_temperature(self.name, t, l) for pipe in incoming_pipes[l])

            self.block.def_mixed_temp = Constraint(self.model.TIME, self.model.lines, rule=_temp_bal_incoming)

            def _temp_bal_outgoing(b, t, l, comp):
                if comp in outgoing_pipes[l]:
                    return p[comp].get_temperature(self.name, t, l) == b.mix_temp[t, l]
                elif comp in outgoing_comps[l]:
                    return c[comp].get_temperature(t, l) == b.mix_temp[t, l]
                else:
                    return Constraint.Skip

            self.block.outgoing_temp_comps = Constraint(self.model.TIME,
                                                        self.model.lines,
                                                        self.comps.keys(),
                                                        rule=_temp_bal_outgoing)
            self.block.outgoing_temp_pipes = Constraint(self.model.TIME,
                                                        self.model.lines,
                                                        pipes.keys(),
                                                        rule=_temp_bal_outgoing)

        else:

            def _heat_bal(b, t):
                return 0 == sum(self.comps[i].get_heat(t) for i in self.comps) \
                            + sum(
                    self.get_pipe(self.graph, edge).get_heat(self.name, t) for edge in pipes.keys())

            self.block.ineq_heat_bal = Constraint(self.model.TIME, rule=_heat_bal)

            def _mass_bal(b, t):
                return 0 == sum(self.comps[i].get_mflo(t) for i in self.comps) \
                            + sum(
                    self.get_pipe(self.graph, edge).get_mflo(self.name, t) for edge in pipes.keys())

            self.block.ineq_mass_bal = Constraint(self.model.TIME, rule=_mass_bal)

    def _make_block(self, model):
        """
        Make a seperate block in the pyomo Concrete model for the Node
        :param model: The model to which it should be added
        :return:
        """
        # TODO Make base class
        assert model is not None, 'Top level model must be initialized first'
        self.model = model
        # If block is already present, remove it
        if self.model.component(self.name) is not None:
            self.model.del_component(self.name)
        self.model.add_component(self.name, Block())
        self.block = self.model.__getattribute__(self.name)

        self.logger.info(
            'Optimization block initialized for {}'.format(self.name))

    def get_mflo(self, t):
        """
        Calculate the mass flow into the network

        :return: mass flow
        """

        # TODO Find something better

        m_flo = 0
        for _, comp in self.comps.items():
            m_flo += comp.get_mflo(t, compiled=False)

        return m_flo

    @staticmethod
    def get_pipe(graph, edgetuple):
        """
        Return Pipe model in specified edge of graph

        :param graph: Graph in which the edge is contained
        :param edgetuple: Tuple representation of edge
        :return:
        """
        return graph.get_edge_data(*edgetuple)['conn'].pipe

    def get_edges(self):
        """
        Collect pipe objects connected to the node

        :return: A dict, values are Pipe objects, names are pipe names
        """

        edges = list(self.graph.in_edges(self.name)) + list(self.graph.out_edges(self.name))
        names = nx.get_edge_attributes(self.graph, 'name')
        pipes = {}
        for edge in edges:
            pipes[names[edge]] = self.get_pipe(self.graph, edge)

        return pipes


class Edge(object):
    def __init__(self, name, edge, start_node, end_node, horizon,
                 time_step, pipe_model, allow_flow_reversal, temperature_driven):
        """
        Connection object between two nodes in a graph

        :param name: Unique identifier of node (str)
        :param edge: Networkx Edge object
        :param start_node: modesto.Node object
        :param stop_node: modesto.Node object
        :param horizon: Horizon of the problem
        :param time_step: Time step between two points of the problem
        :param pipe_model: Type of pipe model to be used
        """

        self.logger = logging.getLogger('modesto.Edge')
        self.logger.info('Initializing Edge {}'.format(name))

        self.name = name
        self.edge = edge

        self.horizon = horizon
        self.time_step = time_step

        self.start_node = start_node
        self.end_node = end_node
        self.length = self.get_length()

        self.temperature_driven = temperature_driven

        self.pipe_model = pipe_model
        self.pipe = self.build(pipe_model, allow_flow_reversal)  # TODO Better structure possible?

    def build(self, pipe_model, allow_flow_reversal):
        """
        Creates the supply and pipe components

        :param pipe_model: The name of the pipe ;odel to be used
        :param allow_flow_reversal: True if flow reversal is allowed
        :return: The pipe object
        """

        self.pipe_model = pipe_model

        def str_to_class(str):
            return reduce(getattr, str.split("."), sys.modules[__name__])

        try:
            cls = str_to_class(pipe_model)
        except AttributeError:
            cls = None

        if cls:
            obj = cls(self.name, self.horizon, self.time_step, self.start_node.name,
                      self.end_node.name, self.length, allow_flow_reversal=allow_flow_reversal,
                      temperature_driven=self.temperature_driven)
        else:
            obj = None

        if obj is None:
            raise ValueError("%s is not a valid class name! (pipe %s)" % (pipe_model, self.name))

        self.logger.info('Pipe model {} added to {}'.format(pipe_model, self.name))

        return obj

    def compile(self, model):

        self.pipe.compile(model)

    def get_length(self):

        sumsq = 0

        for i in ['x', 'y', 'z']:
            sumsq += (self.start_node.get_loc()[i] - self.end_node.get_loc()[i]) ** 2
        return sqrt(sumsq)
