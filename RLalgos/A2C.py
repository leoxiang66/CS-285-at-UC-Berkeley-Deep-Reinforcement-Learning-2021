import gym
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions import Categorical
from collections import deque
import numpy as np

class ActorNetwork(nn.Module):

    def __init__(self, input_size, hidden_size, action_size):
        super(ActorNetwork, self).__init__()
        self.fc1 = nn.Linear(input_size, hidden_size)
        self.fc2 = nn.Linear(hidden_size, hidden_size)
        self.fc3 = nn.Linear(hidden_size, action_size)

    def forward(self, x):
        out = F.relu(self.fc1(x))
        out = F.relu(self.fc2(out))
        out = F.softmax(self.fc3(out), dim=-1)
        return out


class ValueNetwork(nn.Module):

    def __init__(self, input_size, hidden_size, output_size):
        super(ValueNetwork, self).__init__()
        self.fc1 = nn.Linear(input_size, hidden_size)
        self.fc2 = nn.Linear(hidden_size, hidden_size)
        self.fc3 = nn.Linear(hidden_size, output_size)

    def forward(self, x):
        out = F.relu(self.fc1(x))
        out = F.relu(self.fc2(out))
        out = self.fc3(out)
        return out

def make_seed(seed):
    np.random.seed(seed=seed)
    torch.manual_seed(seed=seed)


class A2CAgent:

    def __init__(self, config):
        self.config = config
        self.env = gym.make(config['env_id'])
        make_seed(config['seed'])
        self.env.seed(config['seed'])
        # self.monitor_env = Monitor(self.env, "./gym-results", force=True, video_callable=lambda episode: True)
        self.gamma = config['gamma']

        # Our two networks
        self.value_network = ValueNetwork(self.env.observation_space.shape[0], 16, 1)
        self.actor_network = ActorNetwork(self.env.observation_space.shape[0], 16, self.env.action_space.n)

        # Their optimizers
        self.value_network_optimizer = optim.RMSprop(self.value_network.parameters(),
                                                     lr=config['value_network']['learning_rate'])
        self.actor_network_optimizer = optim.RMSprop(self.actor_network.parameters(),
                                                     lr=config['actor_network']['learning_rate'])

    # Hint: use it during training_batch
    def _returns_advantages(self, rewards, dones, values, next_value):
        """Returns the cumulative discounted rewards at each time step

        Parameters
        ----------
        rewards : array
            An array of shape (batch_size,) containing the rewards given by the env
        dones : array
            An array of shape (batch_size,) containing the done bool indicator given by the env
        values : array
            An array of shape (batch_size,) containing the values given by the value network
        next_value : float
            The value of the next state given by the value network

        Returns
        -------
        returns : array
            The cumulative discounted rewards
        advantages : array
            The advantages
        """

        returns = np.append(np.zeros_like(rewards), [next_value], axis=0)

        for t in reversed(range(rewards.shape[0])):
            returns[t] = rewards[t] + self.gamma * returns[t + 1] * (1 - dones[t])

        returns = returns[:-1]
        advantages = returns - values
        return returns, advantages

    def training_batch(self, epochs, batch_size):
        """Perform a training by batch

        Parameters
        ----------
        epochs : int
            Number of epochs
        batch_size : int
            The size of a batch
        """
        episode_count = 0
        actions = np.empty((batch_size,), dtype=np.int)
        dones = np.empty((batch_size,), dtype=np.bool)
        rewards, values = np.empty((2, batch_size), dtype=np.float)
        observations = np.empty((batch_size,) + self.env.observation_space.shape, dtype=np.float)
        observation = self.env.reset()
        rewards_test = []

        for epoch in range(epochs):
            # Lets collect one batch
            for i in range(batch_size):
                observations[i] = observation
                values[i] = self.value_network(torch.tensor(observation, dtype=torch.float)).detach().numpy()
                policy = self.actor_network(torch.tensor(observation, dtype=torch.float))
                actions[i] = torch.multinomial(policy, 1).detach().numpy()
                observation, rewards[i], dones[i], _ = self.env.step(actions[i])

                if dones[i]:
                    observation = self.env.reset()

            # If our epiosde didn't end on the last step we need to compute the value for the last state
            if dones[-1]:
                next_value = 0
            else:
                next_value = self.value_network(torch.tensor(observation, dtype=torch.float)).detach().numpy()[0]

            # Update episode_count
            episode_count += sum(dones)

            # Compute returns and advantages
            returns, advantages = self._returns_advantages(rewards, dones, values, next_value)

            # Learning step !
            self.optimize_model(observations, actions, returns, advantages)

            # Test it every 50 epochs
            if epoch % 50 == 0 or epoch == epochs - 1:
                rewards_test.append(np.array([self.evaluate() for _ in range(50)]))
                print(
                    f'Epoch {epoch}/{epochs}: Mean rewards: {round(rewards_test[-1].mean(), 2)}, Std: {round(rewards_test[-1].std(), 2)}')

                # Early stopping
                if rewards_test[-1].mean() > 490 and epoch != epochs - 1:
                    print('Early stopping !')
                    break
                observation = self.env.reset()

        # # Plotting
        # r = pd.DataFrame((itertools.chain(*(itertools.product([i], rewards_test[i]) for i in range(len(rewards_test))))), columns=['Epoch', 'Reward'])
        # sns.lineplot(x="Epoch", y="Reward", data=r, ci='sd');

        # print(f'The trainnig was done over a total of {episode_count} episodes')

    def optimize_model(self, observations, actions, returns, advantages):
        actions = F.one_hot(torch.tensor(actions), self.env.action_space.n)
        returns = torch.tensor(returns[:, None], dtype=torch.float)
        advantages = torch.tensor(advantages, dtype=torch.float)
        observations = torch.tensor(observations, dtype=torch.float)

        # MSE for the values
        self.value_network_optimizer.zero_grad()
        values = self.value_network(observations)
        loss_value = 1 * F.mse_loss(values, returns)
        loss_value.backward()
        self.value_network_optimizer.step()

        # Actor loss
        self.actor_network_optimizer.zero_grad()
        policies = self.actor_network(observations)
        loss_policy = ((actions.float() * policies.log()).sum(-1) * advantages).mean()
        loss_entropy = - (policies * policies.log()).sum(-1).mean()
        loss_actor = - loss_policy - 0.0001 * loss_entropy
        loss_actor.backward()
        self.actor_network_optimizer.step()

        return loss_value, loss_actor

    def evaluate(self):
        env = self.env
        observation = env.reset()
        observation = torch.tensor(observation, dtype=torch.float)
        reward_episode = 0
        done = False

        while not done:
            policy = self.actor_network(observation)
            action = torch.multinomial(policy, 1)
            observation, reward, done, info = env.step(int(action))
            observation = torch.tensor(observation, dtype=torch.float)
            reward_episode += reward

        env.close()

        return reward_episode

# #@title Config A2C {display-mode: "form", run: "auto"}
# from pprint import pprint
# env_id = 'CartPole-v1'  #@param ["CartPole-v1", "Acrobot-v1", "MountainCar-v0"]
# value_learning_rate = 0.001  #@param {type: "number"}
# actor_learning_rate = 0.001  #@param {type: "number"}
# gamma = 1  #@param {type: "number"}
# entropy = 1  #@param {type: "number"}
# seed = 1  #@param {type: "integer"}
# #@markdown ---

# config_a2c = {
#     'env_id': env_id,
#     'gamma': gamma,
#     'seed': seed,
#     'value_network': {'learning_rate': value_learning_rate},
#     'actor_network': {'learning_rate': actor_learning_rate},
#     'entropy': entropy
# }

# print("Current config_a2c is:")
# pprint(config_a2c)


# agent = A2CAgent(config_a2c)
# rewards = agent.training_batch(1000, 256)

# agent.evaluate()
