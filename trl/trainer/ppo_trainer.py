# Copyright 2022 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import inspect
import random
import time
import warnings
from typing import List, Optional, Union

import datasets
import torch
import wandb
from accelerate import Accelerator
from datasets import Dataset
from packaging import version
from torch.optim import Adam
from transformers import DataCollatorForLanguageModeling, PreTrainedTokenizer, PreTrainedTokenizerFast

from ..core import (
    WANDB_PADDING,
    clip_by_value,
    entropy_from_logits,
    flatten_dict,
    logprobs_from_logits,
    stack_dicts,
    stats_to_np,
    whiten,
)
from ..models import SUPPORTED_ARCHITECTURES, PreTrainedModelWrapper, create_reference_model
from . import AdaptiveKLController, BaseTrainer, FixedKLController


class PPOTrainer(BaseTrainer):
    """
    The PPOTrainer uses Proximal Policy Optimization to optimise language models.
    """

    def __init__(
        self,
        config,
        model: PreTrainedModelWrapper,
        ref_model: PreTrainedModelWrapper,
        tokenizer: Union[PreTrainedTokenizer, PreTrainedTokenizerFast],
        optimizer: Optional[torch.optim.Optimizer] = None,
        num_shared_layers: Optional[int] = None,
    ):
        """
        Initialize PPOTrainer.

        Args:
            config (`PPOConfig`):
                Configuration object for PPOTrainer. Check the documentation of `PPOConfig` for more details.
            model (`PreTrainedModelWrapper`):
                Hugging Face transformer model with a value head.
            ref_model (`PreTrainedModelWrapper`):
                Hugging Face transformer model with a casual language modelling head. Used for KL penalty
            tokenizer (`transformers.PreTrainedTokenizer`):
                Hugging Face tokenizer
            dataset (Union[`torch.utils.data.Dataset`, `datasets.Dataset`]):
                PyTorch dataset or Hugging Face dataset. If a Hugging Face dataset is passed, the dataset
                will be preprocessed by removing the columns that are not used by the model.
            optimizer (Optional[`torch.optim.Optimizer`]):
                Optimizer used for training. If `None`, the `Adam` is used as default.
            data_collator (Optional[function]):
                Data collator function.
            num_shared_layers (Optional[int]):
                Number of shared layers between the model and the reference model. If `None`, all layers are shared.
                used only if `ref_model` is `None`.
        """
        super().__init__(config)

        # Step 1: Initialize Accelerator
        self.accelerator = Accelerator(log_with="wandb")

        # Step 2: Initialize model, tokenizer, and dataloader
        if not isinstance(model, PreTrainedModelWrapper):
            raise ValueError(
                f"model must be a PreTrainedModelWrapper, got {type(model)} - supported architectures are: {SUPPORTED_ARCHITECTURES}"
            )
        self.model = model

        if isinstance(ref_model, PreTrainedModelWrapper):
            self.ref_model = ref_model
            if num_shared_layers is not None:
                warnings.warn(
                    "num_shared_layers is ignored when ref_model is provided. Two different models are used for the model and the reference model and no layers are shared.",
                    UserWarning,
                )
        elif ref_model is None:
            self.ref_model = create_reference_model(self.model, num_shared_layers=num_shared_layers)
        else:
            raise ValueError(
                f"ref_model must be a PreTrainedModelWrapper or `None`, got {type(ref_model)} - supported architectures are: {SUPPORTED_ARCHITECTURES}"
            )

        self.data_collator = DataCollatorForLanguageModeling(tokenizer, mlm=False)
        if optimizer is None:
            self.optimizer = Adam(self.model.parameters(), lr=self.config.learning_rate)
        else:
            self.optimizer = optimizer

        if self.config.adap_kl_ctrl:
            self.kl_ctl = AdaptiveKLController(self.config.init_kl_coef, self.config.target, self.config.horizon)
        else:
            self.kl_ctl = FixedKLController(self.config.init_kl_coef)

        self.model, self.ref_model, self.optimizer, self.data_collator = self.accelerator.prepare(
            self.model, self.ref_model, self.optimizer, self.data_collator
        )

        # In a distributed setup, only logging needs to be performed on the main process
        # check: https://pytorch.org/docs/stable/generated/torch.nn.parallel.DistributedDataParallel.html
        # or: https://discuss.pytorch.org/t/use-distributed-data-parallel-correctly/82500/11
        self.is_distributed = self.accelerator.distributed_type == "MULTI_GPU"

        # init wandb on the main process:
        if self.accelerator.is_main_process and self.config.log_with_wandb:
            wandb.init(name="run-42", project=self.config.wandb_project, config=config)
            wandb.watch(self.model, log="all")

    def _filter_kwargs(self, kwargs, target_func):
        """
        filter the keyword arguments that are supported by the target function.

        Args:
            kwargs (dict):
                Keyword arguments
            target_func (function):
                Target function
        """
        return {k: v for k, v in kwargs.items() if k in inspect.signature(target_func).parameters.keys()}

    def generate(self, query_tensor: torch.Tensor, **generation_kwargs):
        """
        Generate response given query.

        Args:
            query_tensor (`torch.LongTensor`):
                A tensor of shape (`batch_size`, `seq_len`) containing query tokens.
            gen_kwargs (dict[str, Any]):
                Keyword arguments for generation.

        Returns:
            response (`torch.LongTensor`):
                A tensor of shape (`batch_size`, `gen_len`) containing response tokens.
        """
        response = self.accelerator.unwrap_model(self.model).generate(
            query_tensor.unsqueeze(dim=0), **generation_kwargs
        )

        return response

    def _step_safety_checker(
        self,
        batch_size: int,
        queries: List[torch.LongTensor],
        responses: List[torch.LongTensor],
        scores: List[torch.FloatTensor],
    ):
        """
        Check if the input data is valid for training.

        Args:
            batch_size (int):
                Batch size
            queries (List[`torch.LongTensor`]):
                List of tensors containing the encoded queries of shape (`query_length`)
            responses (List[`torch.LongTensor`]):
                List of tensors containing the encoded responses of shape (`response_length`)
            scores (List[`torch.FloatTensor`]):
                List of tensors containing the scores.
        Returns:
            queries, responses, scores (List[`torch.LongTensor`], List[`torch.LongTensor`], List[`torch.FloatTensor`]):
                The input processed data.
        """
        for name, tensor_list in zip(["queries", "responses", "scores"], [queries, responses, scores]):
            if not isinstance(tensor_list, list):
                raise ValueError(f"{name} must be a list of tensors - got {type(tensor_list)}")
            if not isinstance(tensor_list[0], torch.Tensor):
                raise ValueError(f"Elements in {name} must tensors - got {type(tensor_list[0])}")
            if len(tensor_list) != batch_size:
                raise ValueError(
                    f"Batch size ({batch_size}) does not match number of examples - but got {len(tensor_list)} for: {name}"
                )

        # add queries, scores and responses on the correct device
        queries = [tensor.to(self.accelerator.device) for tensor in queries]
        responses = [tensor.to(self.accelerator.device) for tensor in responses]
        scores = [tensor.to(self.accelerator.device) for tensor in scores]

        # squeeze scores if needed
        for i, score in enumerate(scores):
            if score.dim() > 1:
                raise ValueError(f"Scores must be 1-dimensional - got {score.dim()} for {score}")
            elif score.dim() == 1:
                scores[i] = score.squeeze()

        return queries, responses, scores

    def step(
        self,
        queries: List[torch.LongTensor],
        responses: List[torch.LongTensor],
        scores: List[torch.FloatTensor],
    ):
        """
        Run a PPO optimisation step.

        Args:
            queries (List[`torch.LongTensor`]):
                List of tensors containing the encoded queries of shape (`query_length`)
            responses (List[`torch.LongTensor`]):
                List of tensors containing the encoded responses of shape (`response_length`)
            scores (List[`torch.FloatTensor`]):
                List of tensors containing the scores.

        Returns:
            train_stats (dict[str, Any]):
                a summary of the training statistics
        """

        bs = self.config.batch_size
        
        # Verify the inputs
        queries, responses, scores = self._step_safety_checker(bs, queries, responses, scores)

        timing = dict()
        t0 = time.time()

        # Given the queries and responses, get:
        # Log probabilities of the actions taken
        # Log probabilities for the reference model
        # Predicted values for the actions taken
        t = time.time()
        logprobs, ref_logprobs, values = self.batched_forward_pass(queries, responses)
        timing["time/ppo/forward_pass"] = time.time() - t

        # Gets the rewards (including KL penalty for diverging from reference log probs)
        t = time.time()
        rewards, non_score_reward = self.compute_rewards(scores, logprobs, ref_logprobs)
        timing["time/ppo/compute_rewards"] = time.time() - t

        t = time.time()
        all_stats = []
        idxs = list(range(bs))
        for _ in range(self.config.ppo_epochs):
            random.shuffle(idxs)
            for i in range(bs):
                idx = idxs[i]
                # Train a minibatch. This takes in the logprobs of the actions taken,
                # the predicted values of those actions, the rewards of those actions,
                # the queries, and the responses.
                train_stats = self.train_minibatch(
                    logprobs[idx].unsqueeze(0),
                    values[idx].unsqueeze(0),
                    rewards[idx].unsqueeze(0),
                    queries[idx].unsqueeze(0),
                    responses[idx].unsqueeze(0),
                    torch.cat([queries[idx], responses[idx]]).unsqueeze(0),
                )
                all_stats.append(train_stats)
        timing["time/ppo/optimize_step"] = time.time() - t

        t = time.time()
        train_stats = stack_dicts(all_stats)

        # reshape advantages/ratios such that they are not averaged.
        train_stats["policy/advantages"] = torch.flatten(train_stats["policy/advantages"]).unsqueeze(0)
        train_stats["policy/advantages"] = torch.nan_to_num(train_stats["policy/advantages"], WANDB_PADDING)
        train_stats["policy/ratio"] = torch.flatten(train_stats["policy/ratio"]).unsqueeze(0)

        stats = self.record_step_stats(
            scores=scores,
            logprobs=logprobs,
            ref_logprobs=ref_logprobs,
            non_score_reward=non_score_reward,
            train_stats=train_stats,
            kl_coef=self.kl_ctl.value,
        )
        # Gather/Reduce stats from all processes
        if self.is_distributed:
            stats = self.gather_stats(stats)
        stats = stats_to_np(stats)
        timing["time/ppo/calc_stats"] = time.time() - t

        # Update the KL control - multiply the batch_size by the number of processes
        self.kl_ctl.update(stats["objective/kl"], self.config.batch_size * self.accelerator.num_processes)

        # Log the total ppo time
        timing["time/ppo/total"] = time.time() - t0
        stats.update(timing)
        return stats

    def gather_stats(self, stats):
        """
        Gather stats from all processes. Useful in the context of distributed training.

        Args:
            stats (dict[str, Any]):
            a dictionary of stats to be gathered. The stats should contain torch tensors.

        Returns:
            stats (dict[str, Any]):
                a dictionary of stats with the tensors gathered.
        """
        import torch.distributed as dist

        # Wait for all processes to finish
        dist.barrier()

        for k, v in stats.items():
            if isinstance(v, torch.Tensor):
                dist.all_reduce(v, dist.ReduceOp.SUM)
                v /= self.accelerator.num_processes
            stats[k] = v
        return stats

    def batched_forward_pass(self, queries: torch.Tensor, responses: torch.Tensor):
        """
        Calculate model outputs in multiple batches.

        Args:
            queries (`torch.LongTensor`):
                List of tensors containing the encoded queries, shape (`batch_size`, `query_length`)
            responses (`torch.LongTensor`):
                List of tensors containing the encoded responses, shape (`batch_size`, `response_length`)

        Returns:
            all_logprobs (`torch.FloatTensor`):
                List of tensors containing the logprobs, shape (`batch_size`, `response_length`)
            all_ref_logprobs (`torch.FloatTensor`):
                List of tensors containing the logprobs from the reference model, shape (`batch_size`, `response_length`)
            all_values (`torch.FloatTensor`):
                List of tensors containing the output from the value head, shape (`batch_size`, `response_length`)

        """
        bs = self.config.batch_size
        fbs = self.config.forward_batch_size
        all_logprobs = []
        all_ref_logprobs = []
        all_values = []

        for i in range(int(bs / fbs)):
            # Get queries and responses separately
            query_batch = queries[i * fbs : (i + 1) * fbs]
            response_batch = responses[i * fbs : (i + 1) * fbs]
            # Get query-response strings
            input_ids = self.data_collator([torch.cat([q, r]) for q, r in zip(query_batch, response_batch)])[
                "input_ids"
            ]
            with torch.no_grad():
                logits, _, v = self.model(input_ids)
                ref_logits, _, _ = self.ref_model(input_ids)
                
            # Log probs from everything
            logprobs = logprobs_from_logits(logits[:, :-1, :], input_ids[:, 1:])
            ref_logprobs = logprobs_from_logits(ref_logits[:, :-1, :], input_ids[:, 1:])
            
            # Log probs just for the action that was taken
            for j in range(fbs):
                start = len(query_batch[j]) - 1
                end = len(query_batch[j]) + len(response_batch[j]) - 1
                all_values.append(v[j, start - 1 : end - 1])
                all_logprobs.append(logprobs[j, start:end])
                all_ref_logprobs.append(ref_logprobs[j, start:end])
        return all_logprobs, all_ref_logprobs, all_values

    def train_minibatch(
        self,
        logprobs: torch.FloatTensor,
        values: torch.FloatTensor,
        rewards: torch.FloatTensor,
        query: torch.LongTensor,
        response: torch.LongTensor,
        model_input: torch.LongTensor,
    ):
        """
        Train one PPO minibatch

        Args:
            logprobs (`torch.FloatTensor`):
                Log probabilities of the model, shape [batch_size, response_length]
            values (`torch.FloatTensor`):
                Values of the value head, shape [batch_size, response_length]
            rewards (`torch.FloatTensor`):
                Rewards from the reward model, shape [batch_size, response_length]
            query (`torch.LongTensor`):
                Encoded queries, shape [batch_size, query_length]
            response (`torch.LongTensor`):
                Encoded responses, shape [batch_size, response_length]
            model_input (`torch.LongTensor`):
                Concatenated queries and responses, shape [batch_size, query_length+response_length]

        Returns:
            train_stats (dict[str, `torch.Tensor`]):
                Dictionary of training statistics
        """
        # Find the loss and then optimize. Also record some statistics.
        loss_p, loss_v, train_stats = self.loss(logprobs, values, rewards, query, response, model_input)
        loss = loss_p + loss_v
        self.optimizer.zero_grad()
        self.accelerator.backward(loss)
        t = time.time()
        self.optimizer.step()
        train_stats["time/ppo/optimizer_step"] = torch.Tensor([time.time() - t]).to(self.accelerator.device)
        return train_stats

    def compute_rewards(self, scores: torch.FloatTensor, logprobs: torch.FloatTensor, ref_logprobs: torch.FloatTensor):
        """
        Compute per token rewards from scores and KL-penalty.

        Args:
            scores (`torch.FloatTensor`):
                Scores from the reward model, shape (`batch_size`)
            logprobs (`torch.FloatTensor`):
                Log probabilities of the model, shape (`batch_size`, `response_length`)
            ref_logprobs (`torch.FloatTensor`):
                Log probabilities of the reference model, shape (`batch_size`, `response_length`)
        """
        
        # There is a KL penalty in the *reward* (not the loss). This is controlled by a hyperparam.
        # The KL penalty reward is intrinsic to *each action*, and the score of the completion
        # is intrinsic to the *entire sequence*. Therefore, the completion reward is only added to
        # the end.
        rewards, non_score_rewards = [], []
        for score, logprob, ref_logprob in zip(scores, logprobs, ref_logprobs):
            kl = logprob - ref_logprob
            non_score_reward = -self.kl_ctl.value * kl
            non_score_rewards.append(non_score_reward)
            reward = non_score_reward.clone()
            reward[-1] += score
            rewards.append(reward)
        return rewards, non_score_rewards

    def loss(
        self,
        old_logprobs: torch.FloatTensor,
        values: torch.FloatTensor,
        rewards: torch.FloatTensor,
        query: torch.LongTensor,
        response: torch.LongTensor,
        model_input: torch.LongTensor,
    ):
        """
        Calculate policy and value losses.

        Args:
            old_logprobs (`torch.FloatTensor`):
                Log probabilities of the model, shape (`batch_size`, `response_length`)
            values (`torch.FloatTensor`):
                Values of the value head, shape (`batch_size`, `hidden_dim`)
            rewards (`torch.FloatTensor`):
                Rewards from the reward model, shape (`batch_size`)
            query (`torch.LongTensor`):
                Encoded queries, shape (`batch_size`, `query_length`)
            response (`torch.LongTensor`):
                Encoded responses, shape (`batch_size`, `response_length`)
            model_input (`torch.LongTensor`):
                Concatenated queries and responses, shape (`batch_size`, `query_length+response_length`)
        """
        
        # Generalized Advantage Estimation
        # Lambda is a smoothing parameter (usually 0.95)
        # Gamma is the discount factor (in LLMs, this is 1.00)
        lastgaelam = 0
        advantages_reversed = []
        gen_len = response.shape[1]

        for t in reversed(range(gen_len)):
            nextvalues = values[:, t + 1] if t < gen_len - 1 else 0.0
            # Deviation from true reward. Takes [current reward + discounted future reward (gamma * next)] and
            # compares it to the predicted value. This is the predicted *advantage*.
            delta = (rewards[:, t] + self.config.gamma * nextvalues) - values[:, t]
            # Deviation from true reward as an EMA (gamma * lambda * delta2 + delta1)
            lastgaelam = delta + self.config.gamma * self.config.lam * lastgaelam
            advantages_reversed.append(lastgaelam)
        advantages = torch.stack(advantages_reversed[::-1]).transpose(0, 1)

        # Total returns
        returns = advantages + values
        # Advantages are detached and not used for differentiation
        advantages = whiten(advantages)
        advantages = advantages.detach()

        # Calculate logits and value predictions again
        logits, _, vpred = self.model(model_input)
        logprob = logprobs_from_logits(logits[:, :-1, :], model_input[:, 1:])

        # Only take the values and logprobs for what was generated, not what was provided
        # (don't treat the query as an action that was taken by the model)
        logprob, vpred = logprob[:, -gen_len:], vpred[:, -gen_len - 1 : -1]

        # Deviation of predicted values from true values is clipped by cliprange_value
        vpredclipped = clip_by_value(vpred, values - self.config.cliprange_value, values + self.config.cliprange_value)

        # MSE loss for difference between predicted values and actual returns
        vf_losses1 = (vpred - returns) ** 2
        # MSE loss for difference between predicted values (clipped) and actual returns
        vf_losses2 = (vpredclipped - returns) ** 2
        # For each token, take the maximum of the two losses (clipped or unclipped). Then,
        # average across the tokens.
        vf_loss = 0.5 * torch.mean(torch.max(vf_losses1, vf_losses2))
        vf_clipfrac = torch.mean(torch.gt(vf_losses2, vf_losses1).double())

        # Ratio between probabilities. old_logprobs are the *reference* probabilities.
        ratio = torch.exp(logprob - old_logprobs)

        # Policy gradient loss with clipping
        pg_losses = -advantages * ratio
        pg_losses2 = -advantages * torch.clamp(ratio, 1.0 - self.config.cliprange, 1.0 + self.config.cliprange)

        # Mean across tokens
        pg_loss = torch.mean(torch.max(pg_losses, pg_losses2))
        # Tracking how many were clipped
        pg_clipfrac = torch.mean(torch.gt(pg_losses2, pg_losses).double())

        # Final loss
        loss = pg_loss + self.config.vf_coef * vf_loss

        # Entropy
        entropy = torch.mean(entropy_from_logits(logits))
        # Approximate KL divergence (?)
        approxkl = 0.5 * torch.mean((logprob - old_logprobs) ** 2)
        # Policy KL divergence
        policykl = torch.mean(logprob - old_logprobs)
        # Mean and variance of returns and values
        return_mean, return_var = torch.mean(returns), torch.var(returns)
        value_mean, value_var = torch.mean(values), torch.var(values)
        
        # This loss doesn't include entropy; entropy is instead included in the reward.
        stats = dict(
            loss=dict(policy=pg_loss, value=vf_loss, total=loss),
            policy=dict(
                entropy=entropy,
                approxkl=approxkl,
                policykl=policykl,
                clipfrac=pg_clipfrac,
                advantages=advantages,
                advantages_mean=torch.mean(advantages),
                ratio=ratio,
            ),
            returns=dict(mean=return_mean, var=return_var),
            val=dict(
                vpred=torch.mean(vpred),
                error=torch.mean((vpred - returns) ** 2),
                clipfrac=vf_clipfrac,
                mean=value_mean,
                var=value_var,
            ),
        )
        return pg_loss, self.config.vf_coef * vf_loss, flatten_dict(stats)

    def record_step_stats(self, kl_coef: float, **data):
        """
        Record training step statistics.


        Args:
            kl_coef (`float`):
                KL coefficient
            data (`dict`):
                Dictionary of training step data

        Returns:
            stats (`dict`):
                Dictionary of training step statistics
        """
        kl_list = [logprobs - ref_logprobs for logprobs, ref_logprobs in zip(data["logprobs"], data["ref_logprobs"])]
        mean_kl = torch.mean(torch.stack([torch.sum(kl) for kl in kl_list]))
        mean_entropy = torch.mean(torch.stack([torch.sum(-log_probs) for log_probs in data["logprobs"]]))
        mean_non_score_reward = torch.mean(
            torch.stack([torch.sum(non_score_reward) for non_score_reward in data["non_score_reward"]])
        )
        stats = {
            "objective/kl": mean_kl,
            "objective/kl_dist": kl_list,
            "objective/logprobs": data["logprobs"],
            "objective/ref_logprobs": data["ref_logprobs"],
            "objective/kl_coef": kl_coef,
            "objective/entropy": mean_entropy,
            "ppo/mean_non_score_reward": mean_non_score_reward,
        }

        for k, v in data["train_stats"].items():
            stats[f"ppo/{k}"] = torch.mean(v, axis=0)
        stats["ppo/val/var_explained"] = 1 - stats["ppo/val/error"] / stats["ppo/returns/var"]
        return stats

    def log_stats(
        self,
        stats: dict,
        batch: dict,
        rewards: List[torch.FloatTensor],
    ):
        """
        A function that logs all the training stats. Call it at the end of each epoch.

        Args:
            stats (dict[str, Any]):
                A dictionary of training stats.
            batch (dict[str, Any]):
                A dictionary of batch data, this containes the queries and responses.
            rewards (`List[torch.FloatTensor]`):
                A tensor of rewards.
        """
        # Log only if we are in the main process
        if self.accelerator.is_main_process:
            wandb_logs = {}

            # Log stats
            if not isinstance(rewards, torch.Tensor):
                rewards = torch.tensor(rewards).to(self.accelerator.device)

            if "query" not in batch.keys() and "response" not in batch.keys():
                # warn the user that the game logs will not be logged
                warnings.warn(
                    "The game logs will not be logged because the batch does not contain the keys 'query' and 'response'."
                )
            elif self.config.log_with_wandb:
                table_rows = [list(r) for r in zip(batch["query"], batch["response"], rewards.cpu().tolist())]
                wandb_logs.update({"game_log": wandb.Table(columns=["query", "response", "reward"], rows=table_rows)})
            # All reduce rewards if distributed
            if self.is_distributed:
                import torch.distributed as dist

                dist.barrier()

                dist.all_reduce(rewards, op=torch.distributed.ReduceOp.SUM)
                rewards /= self.accelerator.num_processes

            if self.config.log_with_wandb:
                wandb_logs.update(stats)
                wandb_logs["env/reward_mean"] = torch.mean(rewards).cpu().numpy()
                wandb_logs["env/reward_std"] = torch.std(rewards).cpu().numpy()
                wandb_logs["env/reward_dist"] = rewards.cpu().numpy()
                wandb.log(wandb_logs)
            else:
                stats["env/reward_mean"] = torch.mean(rewards).cpu().numpy()
                stats["env/reward_std"] = torch.std(rewards).cpu().numpy()
                stats["env/reward_dist"] = rewards.cpu().numpy()

        else:
            if self.is_distributed:
                import torch.distributed as dist

                if not isinstance(rewards, torch.Tensor):
                    rewards = torch.tensor(rewards).to(self.accelerator.device)

                dist.barrier()
                dist.all_reduce(rewards, op=torch.distributed.ReduceOp.SUM)
