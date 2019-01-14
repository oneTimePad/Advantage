from enum import Enum
from abc import ABCMeta, abstractmethod
import tensorflow as tf
import numpy as np
import gin
from advantage.buffers.replay_buffers import ReplayBuffer
from advantage.utils.tf_utils import ScopeWrap

""" This module consists of common objectives utilized in Deep
RL. These objectives are pretty much plug-and-play allowing
them to adjust to many popular RL algorithms. MetaObjectives
allow for multiple objectives to be plugged in to build more
complex objectives.

    The main two forms of model-free RL algorithms are:
        Value-Gradient
        Actor-Critic: an approximation to the full return Policy Gradient
"""

class Objective(metaclass=ABCMeta):
    """ Objectives represent the goal/loss
    an Agent is looking to optimization utilizing
    partial trajectory samples
    """

    name_scope = None

    def __init__(self,
                 scope,
                 replay_buffer,
                 func,
                 iteration_of_optimization):

        self._scope = scope

        self._replay_buffer = replay_buffer
        self._element_cls = replay_buffer.element_cls

        self._func = func

        self._iterations_of_optimization = iteration_of_optimization

        self._objective = None

        self._improvement_steps = 0

        self.regularizers = None

    @property
    def scope(self):
        """ property for `_scope`
        """
        return self._scope

    @property
    def func(self):
        """ property for `_func`
        """
        return self._func

    @property
    def replay_buffer(self):
        """ property for `_replay_buffer`
        """
        return self._replay_buffer

    @property
    def element_cls(self):
        """ property for `_element_cls`
        """
        return self._element_cls

    @property
    def improvement_steps(self):
        """ property for `_improvement_steps`
        """
        return self._improvement_steps

    @abstractmethod
    def set_up(self,
               session):
        """ Builds all necessary TF-Graph
        components

            Args:
                session: tf.Session
        """
        raise NotImplementedError()

    @abstractmethod
    def push(self, env_dict):
        """ Accounts for new env_dict
        element

            Args:
                env_dict: sampled from `Environment`
        """
        raise NotImplementedError()

    @abstractmethod
    def optimize(self,
                 session,
                 batch_size):
        """ Performs optimization step
        for `value_func`

            Args:
                session: tf.Session
                batch_size: training batch size
        """
        raise NotImplementedError()

@gin.configurable(blacklist=["scope"])
class ValueGradientObjective(Objective):
    """ Used for an agent
    that need to minimize the Advantage
    or Bellman e`rror. The policy converges
    when it can accurately predict the expected
    reward given a state, value pairs.
    This is also known as the `Value Gradient`
    """

    name_scope = "value_gradient_objective"

    def __init__(self,
                 scope,
                 replay_buffer,
                 discount_factor,
                 value_func,
                 iteration_of_optimization,
                 steps,
                 bootstrap_func):
        """
            Args:
                replay_buffer: This objective uses
                    a `Buffer` as it's source
                    of `Elements`
                element_cls: The cls for the `Element`
                    stored in the `Buffer`
                discount_factor : factor used for exponential reward sum
                value_function : value function
                    to optimize
                iterations_of_optimization: times to update for one
                    `optimization` call
                steps: number of step for bellman operator (or return)

        """

        self._discount_factor = discount_factor
        self._steps = steps
        self._bootstrap_func = bootstrap_func

        self._waiting_buffer = ReplayBuffer(replay_buffer.element_cls,
                                            steps)

        super().__init__(self,
                         scope,
                         replay_buffer,
                         value_func,
                         iteration_of_optimization)

    @property
    def bootstrap_func(self):
        """ property for `_bootstrap_func`
        """
        return self._bootstrap_func

    def _add_reward(self, reward):
        """ Integrates `reward` into the
        discounted sum kept by all `Elements`
        in the `_waiting_buffer`

            Args:
                reward: reward to integrat, from
                    the latest `Element`
        """

        buffer_len = len(self._waiting_buffer)
        elements = self._waiting_buffer.sample(buffer_len)

        factors = np.power(self._discount_factor, np.arange(1, buffer_len)[-1:])
        discounted = factors * reward
        elements.n_step_return += discounted

    def set_up(self, session):

        self._func.set_up(session)

        with self._scope():
            # the bellman values for training value_func
            bellman_plh = tf.placeholder(shape=[None, 1],
                                         dtype=tf.float32,
                                         name="bellman_target_plh")

            self._func.add_target_placeholder(bellman_plh)

            self._objective = (1/2) * tf.square(bellman_plh - self._func.func)
            if self.regularizers:
                self._objective += sum(self.regularizers)

            self._objective = tf.reduce_mean(self._objective,
                                             name="objective")

            self._func.minimize(self._objective)

    def push(self, env_dict):
        """ Accounts for new env_dict
        element and computes discounted returns
        for elements in `_waiting_buffer` and
        pushing them to `_replay_buffer` when their
        returns are fully computed
            Args:
                env_dict: sampled from `Environment`
        """

        element = self._element_cls.make_element(env_dict)

        if self._waiting_buffer:
            self._add_reward(element.reward)

        buffer_len = len(self._waiting_buffer)

        if element.done or buffer_len == self._steps:
            if not element.done:
                boostrap_func = self.bootstrap_func
                self._add_reward(boostrap_func([element.next_state]))

            self._replay_buffer.push(self._waiting_buffer.sample(buffer_len))
            self._waiting_buffer.clear()

        else:
            self._waiting_buffer.push(element)

    def optimize(self,
                 session,
                 batch_size):
        for batch in self._replay_buffer.sample_batches(batch_size,
                                                        self._iterations_of_optimization):

            self._func.update(session,
                              {"state": batch.state},
                              {"bellman_target_plh": batch.n_step_return})

        self._improvement_steps += 1

@gin.configurable(blacklist=["scope"])
class DecoupledValueGradientObjective(ValueGradientObjective):
    """ Represents a Bellman objective
    in which the bootstrapping function is of the same
    architecture and type as the on-policy function.
    Thus syncing is required.
    """

    name_scope = "decoupled_value_gradient_objective"

    def __init__(self,
                 scope,
                 replay_buffer,
                 discount_factor,
                 value_func,
                 iteration_of_optimization,
                 steps,
                 sync_period):
        """
            Args:
                bootstrap_func: function for bootstrapping
        """
        self._copy = None

        self._sync_period = sync_period

        self._optimization_calls = 0

        bootstrap_scope = ScopeWrap.build(scope,
                                         "target/{0}".format(value_func.name_scope))

        bootstrap_func = value_func.copy_obj(bootstrap_scope)

        super().__init__(scope,
                         replay_buffer,
                         discount_factor,
                         value_func,
                         iteration_of_optimization,
                         steps,
                         bootstrap_func)

    def set_up(self, session):

        super().set_up(session)

        self._bootstrap_func.set_up(session)

        self._copy = self._bootstrap_func.make_copy_op(session,
                                                       self._func)

    def sync(self):
        """ Synchronizes `bootstrap_func`
        and `value_func` parameters
        """
        self._copy()

    def optimize(self,
                 session,
                 batch_size):

        super().optimize(session, batch_size)

        if self._optimization_calls % self._sync_period == 0:
            self.sync()
        else: # don't count improvement (decrement from super call)
            self._improvement_steps -= 1

@gin.configurable(blacklist=["scope"])
class PolicyGradientObjective(Objective):
    """ Contains information related
    to specifying an objective for an
    `Actor` or Policy related objective
    """

    name_scope = "policy_gradient_objective"

    def __init__(self,
                 scope,
                 replay_buffer,
                 policy_func,
                 policy_return,
                 iteration_of_optimization,
                 from_gradient=False):

        self._policy_return = policy_return

        self._replay_buffer = replay_buffer

        self._from_gradient = from_gradient

        self._action_taken_plh = None
        self._next_state_plh = None

        super().__init__(self,
                         scope,
                         replay_buffer,
                         policy_func,
                         iteration_of_optimization)


    def action_taken_plh(self, action_shape=None):
        """fetching a placeholder for taking the selected
        action in the expected_return
            Args:
                action_shape: shape for action placeholder

            Returns:
                placeholder
        """
        if not self._action_taken_plh:
            with self._scope():
                self._action_taken_plh = tf.placeholder(shape=[None, action_shape],
                                                        dtype=tf.float32,
                                                        name="action_taken")
                self._func.add_target_placeholder(self._action_taken_plh)

        return self._action_taken_plh

    def next_state_plh(self, state_shape=None):
        """fetching a placeholder for taking the next state
        in the expected_return
            Args:
                state_shape: shape for next_state placeholder

            Returns:
                placeholder
        """
        if not self._next_state_plh:
            with self._scope():
                self._next_state_plh = tf.placeholder(shape=[None, state_shape],
                                                      dtype=tf.float32,
                                                      name="next_state")
                self._func.add_target_placeholder(self._next_state_plh)

        return self._next_state_plh

    def set_up(self, session):

        self._func.set_up(session)

        with self._scope():

            if self._from_gradient:

                gradients = self._func.from_gradient_func(self._policy_return)
            else:
                if self.regularizers:
                    self._objective += sum(self.regularizers)

                expected_return = tf.reduce_mean(self._policy_return,
                                                 name="reduce_mean_return")
                gradients = self._func.gradients(expected_return)

            self._func.apply_gradients(gradients)

    def push(self, env_dict):
        element = self._element_cls.make_element(env_dict)
        self._replay_buffer.push(element)

    def optimize(self,
                 session,
                 batch_size):

        for batch in self._replay_buffer.sample_batches(batch_size,
                                                        self._iterations_of_optimization):

            self._func.update(session,
                              {"state": batch.state},
                              {"action_taken": batch.action,
                               "next_state": batch.next_state})

        self._improvement_steps += 1


class Objectives(Enum):
    """Possible Objectives to select
    """
    VALUE_GRADIENT = ValueGradientObjective
    DECOUPLED_VALUE_GRADIENT = DecoupledValueGradientObjective
    POLICY_GRADIENT = PolicyGradientObjective