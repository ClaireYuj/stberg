import torch
import torch.nn as nn
from torch.optim import Adam, RMSprop
import numpy as np
import random
from copy import deepcopy
from numpy import savetxt
from numpy import loadtxt

from src.ReplayBuffer import PrioritizedReplayBuffer
from env.MP_HRL_Env import Env as Env
import torch.nn.functional as F


class CCMADDPG(object):
    def __init__(self,n_agents, state_dim, action_dim, seq_len, action_lower_bound,e_dim,
                 action_higher_bound,memory_capacity=10000, pred_len=2,
                 InfdexofResult=0,
                 target_tau=1, reward_gamma=0.99, reward_scale=1., done_penalty=None,
                 actor_output_activation=torch.tanh, actor_lr=0.0001, critic_lr=0.0001,
                 optimizer_type="adam", max_grad_norm=True, batch_size=1, episodes_before_train=64,
                 epsilon_start=1, epsilon_end=0.01, epsilon_decay=None, use_cuda=True, Benchmarks_mode=None):
        super(CCMADDPG, self).__init__()
        self.n_agents = n_agents
        self.pred_len = pred_len
        self.state_dim = state_dim
        self.action_dim = e_dim
        self.action_lower_bound = action_lower_bound
        self.action_higher_bound = action_higher_bound
        self.n_episodes = 0
        self.seq_len = seq_len

        self.memory = PrioritizedReplayBuffer(memory_capacity,self.state_dim, self.action_dim, self.n_agents, self.seq_len, pred_len=pred_len,mode='follower')
        self.actor_output_activation = actor_output_activation
        self.actor_lr = actor_lr
        self.critic_lr = critic_lr
        self.optimizer_type = optimizer_type
        self.max_grad_norm = max_grad_norm
        self.batch_size = 1
        # params for epsilon greedy
        self.device = torch.device("cuda:0")
        self.reward_scale = 1.
        self.reward_gamma = 0.95

        self.use_cuda = use_cuda and torch.cuda.is_available()
        self.target_tau = target_tau
        # self.actors = [ActorNetwork(self.state_dim, e_dim,self.action_dim, self.actor_output_activation).to(self.device)] * self.n_agents
        self.actors = [ActorNetwork(self.state_dim, e_dim,self.action_dim, self.actor_output_activation).to(self.device)] * self.n_agents

        critic_state_dim = self.n_agents * self.state_dim
        critic_action_dim = self.n_agents * self.action_dim
        self.critics = [CriticNetwork(critic_state_dim, critic_action_dim).to(self.device)] * 1

        # to ensure target network and learning network has the same weights
        self.actors_target = deepcopy(self.actors)
        self.critics_target = deepcopy(self.critics)
        if optimizer_type == "adam":
            self.actors_optimizer = [Adam(a.parameters(), lr=self.actor_lr) for a in self.actors]
            self.critics_optimizer = [Adam(c.parameters(), lr=self.critic_lr) for c in self.critics]
        elif optimizer_type == "rmsprop":
            self.actors_optimizer = [RMSprop(a.parameters(), lr=self.actor_lr) for a in self.actors]
            self.critics_optimizer = [RMSprop(c.parameters(), lr=self.critic_lr) for c in self.critics]
        # if self.use_cuda:
        #     for i in range(self.n_agents):
        #         self.actors[i].cuda()
        #         self.critics[i].cuda()
        #         self.actors_target[i].cuda()
        #         self.critics_target[i].cuda()
        self.eval_episode_rewards = []
        self.server_episode_constraint_exceeds = []
        self.energy_episode_constraint_exceeds = []
        self.time_episode_constraint_exceeds = []
        self.eval_step_rewards = []
        self.mean_rewards = []

        self.episodes = []
        self.Training_episodes = []

        self.Training_episode_rewards = []
        self.Training_step_rewards = []

        self.InfdexofResult = InfdexofResult
        # self.save_models('./checkpoint/Benchmark_'+str(self.Benchmarks_mode)+'_checkpoint'+str(self.InfdexofResult)+'.pth')
        self.results = []
        self.Training_results = []
        self.serverconstraints = []
        self.energyconstraints = []
        self.timeconstraints = []


    def _soft_update_target(self, target, source):
        for t, s in zip(target.parameters(), source.parameters()):
            t.data.copy_(
                (1. - self.target_tau) * t.data + self.target_tau * s.data)


    # train on a sample batch
    def train(self):
        # do not train until exploration is enough

        tryfetch = 0
        idxs, states_var, actor_actions_var, rewards_var, next_states_var, batch_log, batch_val, dones_var = self.memory.sample_buffer(
            self.batch_size)

        critic_loss_list = []
        errors = torch.zeros((self.batch_size)).to(self.device)
        loss = []
        for i in range(self.batch_size):
            # bool to binary
            states_var = states_var[i].view(-1, self.n_agents, self.state_dim)
            actor_actions_var = actor_actions_var[i].view(-1, self.n_agents, self.action_dim)
            rewards_var = rewards_var[i].view(-1, self.n_agents, 1)
            next_states_var = next_states_var[i].view(-1, self.n_agents, self.state_dim)
            dones_var = dones_var[i].view(-1, 1)
            whole_states_var = states_var[i].view(-1, self.n_agents * self.state_dim)
            whole_actor_actions_var = actor_actions_var[i].view(-1, self.n_agents * self.action_dim)
            whole_next_states_var = next_states_var[i].view(-1, self.n_agents * self.state_dim)

            nextactor_actions = []
            # Calculate next target actions for each agent
            for agent_id in range(self.n_agents):
                next_action_var, _ = self.actors_target[agent_id](next_states_var[:, agent_id, :])
                if self.use_cuda:
                    nextactor_actions.append(next_action_var)
                else:
                    nextactor_actions.append(next_action_var)
            # Concatenate the next target actions into a single tensor
            nextactor_actions_var = torch.cat(nextactor_actions, dim=1)
            nextactor_actions_var = nextactor_actions_var.view(-1, actor_actions_var.size(1), actor_actions_var.size(2))
            whole_nextactor_actions_var = nextactor_actions_var.view(-1, self.n_agents * self.action_dim)

            # common critic
            agent_id = 0
            target_q = []
            current_q = []
            for b in range(self.batch_size):
                # target prediction
                tar_perQ = self.critics_target[agent_id](whole_next_states_var[b], whole_nextactor_actions_var[b])[-self.pred_len:]
                tar = self.reward_scale * rewards_var[b, agent_id, :] + self.reward_gamma * tar_perQ * (1. - dones_var[b])
                target_q.append(tar)
                curr_perQ = self.critics[agent_id](whole_states_var[b], whole_actor_actions_var[b])[-self.pred_len:]
                current_q.append(curr_perQ)
                errors[b] = errors[b] + F.mse_loss(curr_perQ, tar)

            # update critic network
            current_q = torch.stack(current_q, dim=0)
            target_q = torch.stack(target_q, dim=0)
            # c_loss = nn.MSELoss()(current_q, target_q)
            # c_loss.requires_grad_(True)
            # critic_loss_list.append(c_loss)
            critic_loss = nn.MSELoss()(current_q, target_q)
            # update target
            self.critics_optimizer[0].zero_grad()

            critic_loss.backward()
            loss.append(critic_loss)
            if self.max_grad_norm is not None:
                torch.nn.utils.clip_grad_norm_(self.critics[0].parameters(), self.max_grad_norm)
            self.critics_optimizer[0].step()
            self._soft_update_target(self.critics_target[0], self.critics[0])

            # different actors
            for agent_id in range(self.n_agents):
                newactor_actions = []
                # Calculate new actions for each agent
                for agent_id in range(self.n_agents):
                    newactor_action_var, _ = self.actors[agent_id](states_var[:, agent_id, :])
                    if self.use_cuda:
                        newactor_actions.append(
                            newactor_action_var)  # newactor_actions.append(newactor_action_var.data.cpu())
                    else:
                        newactor_actions.append(newactor_action_var)  # newactor_actions.append(newactor_action_var.data)
                # Concatenate the new actions into a single tensor
                newactor_actions_var = torch.cat(newactor_actions, dim=1)
                newactor_actions_var = newactor_actions_var.view(-1, actor_actions_var.size(1), actor_actions_var.size(2))
                whole_newactor_actions_var = newactor_actions_var.view(-1, self.n_agents * self.action_dim)
                actor_loss = []
                for b in range(self.batch_size):
                    perQ = self.critics[0](whole_states_var[b], whole_newactor_actions_var[b])[-self.pred_len:]
                    actor_loss.append(perQ)
                actor_loss = torch.stack(actor_loss, dim=0)
                actor_loss = - actor_loss.mean()
                actor_loss.requires_grad_(True)
                self.actors_optimizer[agent_id].zero_grad()
                actor_loss.backward()
                loss.append(actor_loss)

                if self.max_grad_norm is not None:
                    torch.nn.utils.clip_grad_norm_(self.actors[agent_id].parameters(), self.max_grad_norm)
                self.actors_optimizer[agent_id].step()
                self._soft_update_target(self.actors_target[agent_id], self.actors[agent_id])  # update target network
                idx = idxs[i]
                # print("errors",idx,errors)

                self.memory.update_priorities(idx, errors[i])
        loss = torch.mean(torch.stack(loss), dim=0)
        return loss

    def check_parameter_difference(self, model, loaded_state_dict):
        current_state_dict = model.state_dict()
        for name, param in current_state_dict.items():
            if name in loaded_state_dict:
                if not torch.equal(param, loaded_state_dict[name]):
                    # print(f"Parameter '{name}' has changed since the last checkpoint.")
                    return 1
                else:
                    # print(f"Parameter '{name}' has not changed since the last checkpoint.")
                    return 0
            else:
                print("Parameter '" + name + "' is not present in the loaded checkpoint.")
                exit()

    def getactionbound(self, a, b, x, i):
        x = (x - a) * (self.action_higher_bound[i] - self.action_lower_bound[i]) / (b - a) \
            + self.action_lower_bound[i]
        return x

    # choose an action based on state with random noise added for exploration in training
    def choose_actions(self, s):
        n_agent, seq_len, f = s.shape
        actor_action = torch.zeros((self.n_agents, seq_len, self.action_dim))
        des_edge =  torch.zeros((self.n_agents, seq_len, 1))
        for agent_id in range(self.n_agents):
            action, edge_id = self.actors[agent_id](s[agent_id,:,:])
            actor_action[agent_id, :, :] = action
            des_edge[agent_id, :, :] = edge_id
        val = torch.tensor([0.])
        return actor_action, des_edge, val

    def evaluateAtTraining(self, EVAL_EPISODES):
        # print(self.eval_episode_rewards)
        mean_reward = np.mean(np.array(self.Training_episode_rewards))
        self.Training_episode_rewards = []
        # self.mean_rewards.append(mean_reward)# to be plotted by the main function
        self.Training_episodes.append(self.n_episodes + 1)
        self.Training_results.append(mean_reward)
        arrayresults = np.array(self.Training_results)
        savetxt('./CSV/AtTraining/' + str(self.Benchmarks_mode) + str(self.InfdexofResult) + '.csv', arrayresults)
        # print("Episode:", self.n_episodes, "Episodic Reward:  Min mean Max : ", np.min(arrayresults), mean_reward, np.max(arrayresults))

    def save_model(self, path='marl_model.pth'):
        save_data = {
            'actors': [actor.state_dict() for actor in self.actors],
            'critics': [critic.state_dict() for critic in self.critics],
            'actor_optimizers': [actor_opt.state_dict() for actor_opt in self.actors_optimizer],
            'critic_optimizers': [critic_opt.state_dict() for critic_opt in self.critics_optimizer]
        }
        torch.save(save_data, path)
        print(f"save model to : {path}")

    def load_model(self,path='marl_model.pth'):
        checkpoint = torch.load(path)
        for actor, state_dict in zip(self.actors, checkpoint['actors']):
            actor.load_state_dict(state_dict)
        for actor_opt, state_dict in zip(self.actors_optimizer, checkpoint['actor_optimizers']):
            actor_opt.load_state_dict(state_dict)
        for critic, state_dict in zip(self.critics, checkpoint['critics']):
            critic.load_state_dict(state_dict)
        for critic_opt, state_dict in zip(self.critics_optimizer, checkpoint['critic_optimizers']):
            critic_opt.load_state_dict(state_dict)
        print(f"load model from {path} ")

class ActorNetwork(nn.Module):
    """
    A network for actor
    """

    def __init__(self, state_dim, e_dim, output_size, output_activation, init_w=3e-3):
        super(ActorNetwork, self).__init__()
        self.e_dim = e_dim
        self.fc1 = nn.Linear(state_dim, 64)
        self.fc2 = nn.Linear(64, 32)
        self.fc3 = nn.Linear(32, e_dim)

        # self.fc3.weight.data.uniform_(-init_w, init_w)
        # self.fc3.bias.data.uniform_(-init_w, init_w)
        # activation function for the output
        self.output_activation = output_activation

    def __call__(self, state):
        out = nn.functional.sigmoid(self.fc1(state))
        out = nn.functional.sigmoid(self.fc2(out))
        if self.output_activation == nn.functional.softmax:
            out = self.output_activation(self.fc3(out), dim=-1)
        else:
            out = self.output_activation(self.fc3(out))
        probs = F.softmax(out)
        act = torch.argmax(probs, dim=1).unsqueeze(1)
        return out, act


class CriticNetwork(nn.Module):
    """
    A network for critic
    """

    def __init__(self, state_dim, action_dim, output_size=1, init_w=3e-3):
        super(CriticNetwork, self).__init__()
        self.fc1 = nn.Linear(state_dim + action_dim,
                             512)  # state_dim + action_dim = for the combined, equivalent of it for the per agent, and 1 for distinguisher
        self.fc2 = nn.Linear(512, 128)
        self.fc3 = nn.Linear(128, output_size)

        self.fc3.weight.data.uniform_(-init_w, init_w)
        self.fc3.bias.data.uniform_(-init_w, init_w)

    def __call__(self, state, action):
        out = torch.cat([state, action], 0)
        out = nn.functional.sigmoid(self.fc1(out))
        out = nn.functional.sigmoid(self.fc2(out))
        out = self.fc3(out)
        return out

