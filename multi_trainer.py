"""
PyTorch policy class used for PPO.
"""
import gym
import logging
import numpy as np
from typing import Dict, List, Optional, Type, Union

import ray
from ray.rllib.agents.ppo.ppo_tf_policy import setup_config
from ray.rllib.agents.trainer_template import build_trainer
from ray.rllib.evaluation.episode import MultiAgentEpisode
from ray.rllib.evaluation.postprocessing import compute_advantages, \
    Postprocessing
from ray.rllib.models.modelv2 import ModelV2
from ray.rllib.models.torch.torch_action_dist import TorchDistributionWrapper
from ray.rllib.policy.policy import Policy
from ray.rllib.policy.policy_template import build_policy_class
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.policy.torch_policy import EntropyCoeffSchedule, \
    LearningRateSchedule
from ray.rllib.utils.framework import try_import_torch
from ray.rllib.utils.torch_ops import apply_grad_clipping, \
    convert_to_torch_tensor, explained_variance, sequence_mask
from ray.rllib.utils.typing import TensorType, TrainerConfigDict, AgentID

torch, nn = try_import_torch()

logger = logging.getLogger(__name__)

def compute_gae_for_sample_batch(
        policy: Policy,
        sample_batch: SampleBatch,
        other_agent_batches: Optional[Dict[AgentID, SampleBatch]] = None,
        episode: Optional[MultiAgentEpisode] = None) -> SampleBatch:
    """Adds GAE (generalized advantage estimations) to a trajectory.
    The trajectory contains only data from one episode and from one agent.
    - If  `config.batch_mode=truncate_episodes` (default), sample_batch may
    contain a truncated (at-the-end) episode, in case the
    `config.rollout_fragment_length` was reached by the sampler.
    - If `config.batch_mode=complete_episodes`, sample_batch will contain
    exactly one episode (no matter how long).
    New columns can be added to sample_batch and existing ones may be altered.
    Args:
        policy (Policy): The Policy used to generate the trajectory
            (`sample_batch`)
        sample_batch (SampleBatch): The SampleBatch to postprocess.
        other_agent_batches (Optional[Dict[PolicyID, SampleBatch]]): Optional
            dict of AgentIDs mapping to other agents' trajectory data (from the
            same episode). NOTE: The other agents use the same policy.
        episode (Optional[MultiAgentEpisode]): Optional multi-agent episode
            object in which the agents operated.
    Returns:
        SampleBatch: The postprocessed, modified SampleBatch (or a new one).
    """

    samplebatch_infos_rewards = None
    if not sample_batch[SampleBatch.INFOS].dtype == "float32":
        # the trajectory view API will pass empty data in the first call, we have to ignore it...
        samplebatch_infos = SampleBatch.concat_samples(
            [
                SampleBatch({k: [v] for k, v in s.items()})
                for s in sample_batch[SampleBatch.INFOS]
            ]
        )
        samplebatch_infos_rewards = SampleBatch.concat_samples(
            [
                SampleBatch({str(k): [v] for k, v in s.items()})
                for s in samplebatch_infos["rewards"]
            ]
        )
        # samplebatch_infos_dones = SampleBatch.concat_samples([SampleBatch({str(k): [v] for k, v in s.items()}) for s in samplebatch_infos['dones']])

    # We have to access these elements here, otherwise the trajectory view API will not add them to the next runs as completed is true
    for j in range(policy.num_state_tensors()):
        sample_batch["state_out_{}".format(j)]
    sample_batch[SampleBatch.NEXT_OBS]
    sample_batch[SampleBatch.ACTIONS]
    sample_batch[SampleBatch.REWARDS]

    # samplebatches for each agents
    batches = []
    for i in ["0", "1", "2"]:  # samplebatch_infos_rewards.keys():
        sample_batch_agent = sample_batch.copy()
        # sample_batch_agent["DONE"] = samplebatch_infos_dones[i]
        sample_batch_agent[SampleBatch.REWARDS] = (
            samplebatch_infos_rewards[i]
            if samplebatch_infos_rewards is not None
            else sample_batch[SampleBatch.INFOS]
        )
        sample_batch_agent[SampleBatch.ACTIONS] = sample_batch[SampleBatch.ACTIONS][
            :, int(i) : (int(i) + 1)
        ]
        sample_batch_agent[SampleBatch.VF_PREDS] = sample_batch[SampleBatch.VF_PREDS][
            :, int(i)
        ]

        # Trajectory is actually complete -> last r=0.0.
        if sample_batch[SampleBatch.DONES][-1]:
            last_r = 0.0
        # Trajectory has been truncated -> last r=VF estimate of last obs.
        else:
            # Input dict is provided to us automatically via the Model's
            # requirements. It's a single-timestep (last one in trajectory)
            # input_dict.
            # Create an input dict according to the Model's requirements.
            input_dict = policy.model.get_input_dict(
                sample_batch, index="last")
            last_r = policy._value(**input_dict)[int(i)].cpu()

        # Adds the policy logits, VF preds, and advantages to the batch,
        # using GAE ("generalized advantage estimation") or not.
        batches.append(
            compute_advantages(
                sample_batch_agent,
                last_r,
                policy.config["gamma"],
                policy.config["lambda"],
                use_gae=policy.config["use_gae"],
                use_critic=policy.config.get("use_critic", True)
            )
        )

    # Now take original samplebatch and overwrite following elements as a concatenation of these
    for k in [
        SampleBatch.REWARDS,
        SampleBatch.VF_PREDS,
        Postprocessing.ADVANTAGES,
        Postprocessing.VALUE_TARGETS,
    ]:
        sample_batch[k] = np.stack([b[k] for b in batches], axis=-1)

    return sample_batch


def ppo_surrogate_loss(
        policy: Policy, model: ModelV2,
        dist_class: Type[TorchDistributionWrapper],
        train_batch: SampleBatch) -> Union[TensorType, List[TensorType]]:
    """Constructs the loss for Proximal Policy Objective.
    Args:
        policy (Policy): The Policy to calculate the loss for.
        model (ModelV2): The Model to calculate the loss for.
        dist_class (Type[ActionDistribution]: The action distr. class.
        train_batch (SampleBatch): The training data.
    Returns:
        Union[TensorType, List[TensorType]]: A single loss tensor or a list
            of loss tensors.
    """
    logits, state = model.from_batch(train_batch, is_training=True)
    curr_action_dist = dist_class(logits, model)

    # RNN case: Mask away 0-padded chunks at end of time axis.
    if state:
        B = len(train_batch["seq_lens"])
        max_seq_len = logits.shape[0] // B
        mask = sequence_mask(
            train_batch["seq_lens"],
            max_seq_len,
            time_major=model.is_time_major())
        mask = torch.reshape(mask, [-1])
        num_valid = torch.sum(mask)

        def reduce_mean_valid(t):
            return torch.sum(t[mask]) / num_valid

    # non-RNN case: No masking.
    else:
        mask = None
        reduce_mean_valid = torch.mean

    loss_data = []

    curr_action_dist = dist_class(logits, model)
    prev_action_dist = dist_class(train_batch[SampleBatch.ACTION_DIST_INPUTS],
                                  model)

    action_kl = prev_action_dist.kl(curr_action_dist)
    mean_kl = reduce_mean_valid(torch.sum(action_kl, axis=1))

    for i in range(len(train_batch[SampleBatch.VF_PREDS][0])):
        logp_ratio = torch.exp(
            curr_action_dist.logp(train_batch[SampleBatch.ACTIONS])[:, i] -
            train_batch[SampleBatch.ACTION_LOGP][:, i])

        curr_entropy = curr_action_dist.entropy()[:, i]
        mean_entropy = reduce_mean_valid(curr_entropy)

        surrogate_loss = torch.min(
            train_batch[Postprocessing.ADVANTAGES][..., i] * logp_ratio,
            train_batch[Postprocessing.ADVANTAGES][..., i] * torch.clamp(
                logp_ratio, 1 - policy.config["clip_param"],
                1 + policy.config["clip_param"]))
        mean_policy_loss = reduce_mean_valid(-surrogate_loss)

        if policy.config["use_gae"]:
            prev_value_fn_out = train_batch[SampleBatch.VF_PREDS][..., i]
            value_fn_out = model.value_function()[..., i]
            vf_loss1 = torch.pow(
                value_fn_out - train_batch[Postprocessing.VALUE_TARGETS][..., i], 2.0)
            vf_clipped = prev_value_fn_out + torch.clamp(
                value_fn_out - prev_value_fn_out, -policy.config["vf_clip_param"],
                policy.config["vf_clip_param"])
            vf_loss2 = torch.pow(
                vf_clipped - train_batch[Postprocessing.VALUE_TARGETS][..., i], 2.0)
            vf_loss = torch.max(vf_loss1, vf_loss2)
            mean_vf_loss = reduce_mean_valid(vf_loss)
            total_loss = reduce_mean_valid(
                -surrogate_loss + policy.kl_coeff * action_kl[:, i] +
                policy.config["vf_loss_coeff"] * vf_loss -
                policy.entropy_coeff * curr_entropy)
        else:
            mean_vf_loss = 0.0
            total_loss = reduce_mean_valid(-surrogate_loss +
                                           policy.kl_coeff * action_kl[:, i] -
                                           policy.entropy_coeff * curr_entropy)

        # Store stats in policy for stats_fn.
        loss_data.append(
            {
                "total_loss": total_loss,
                "mean_policy_loss": mean_policy_loss,
                "mean_vf_loss": mean_vf_loss,
                "mean_entropy": mean_entropy,
            }
        )

    policy._total_loss = (torch.sum(torch.stack([o["total_loss"] for o in loss_data])),)
    policy._mean_policy_loss = torch.mean(
        torch.stack([o["mean_policy_loss"] for o in loss_data])
    )
    policy._mean_vf_loss = torch.mean(
        torch.stack([o["mean_vf_loss"] for o in loss_data])
    )
    policy._mean_entropy = torch.mean(
        torch.stack([o["mean_entropy"] for o in loss_data])
    )
    policy._vf_explained_var = explained_variance(
        train_batch[Postprocessing.VALUE_TARGETS],
        policy.model.value_function())
    policy._mean_kl = mean_kl

    return policy._total_loss


def kl_and_loss_stats(policy: Policy,
                      train_batch: SampleBatch) -> Dict[str, TensorType]:
    """Stats function for PPO. Returns a dict with important KL and loss stats.
    Args:
        policy (Policy): The Policy to generate stats for.
        train_batch (SampleBatch): The SampleBatch (already) used for training.
    Returns:
        Dict[str, TensorType]: The stats dict.
    """
    return {
        "cur_kl_coeff": policy.kl_coeff,
        "cur_lr": policy.cur_lr,
        "total_loss": policy._total_loss,
        "policy_loss": policy._mean_policy_loss,
        "vf_loss": policy._mean_vf_loss,
        "vf_explained_var": policy._vf_explained_var,
        "kl": policy._mean_kl,
        "entropy": policy._mean_entropy,
        "entropy_coeff": policy.entropy_coeff,
    }


def vf_preds_fetches(
        policy: Policy, input_dict: Dict[str, TensorType],
        state_batches: List[TensorType], model: ModelV2,
        action_dist: TorchDistributionWrapper) -> Dict[str, TensorType]:
    """Defines extra fetches per action computation.
    Args:
        policy (Policy): The Policy to perform the extra action fetch on.
        input_dict (Dict[str, TensorType]): The input dict used for the action
            computing forward pass.
        state_batches (List[TensorType]): List of state tensors (empty for
            non-RNNs).
        model (ModelV2): The Model object of the Policy.
        action_dist (TorchDistributionWrapper): The instantiated distribution
            object, resulting from the model's outputs and the given
            distribution class.
    Returns:
        Dict[str, TensorType]: Dict with extra tf fetches to perform per
            action computation.
    """
    # Return value function outputs. VF estimates will hence be added to the
    # SampleBatches produced by the sampler(s) to generate the train batches
    # going into the loss function.
    return {
        SampleBatch.VF_PREDS: policy.model.value_function(),
    }


class KLCoeffMixin:
    """Assigns the `update_kl()` method to the PPOPolicy.
    This is used in PPO's execution plan (see ppo.py) for updating the KL
    coefficient after each learning step based on `config.kl_target` and
    the measured KL value (from the train_batch).
    """

    def __init__(self, config):
        # The current KL value (as python float).
        self.kl_coeff = config["kl_coeff"]
        # Constant target value.
        self.kl_target = config["kl_target"]

    def update_kl(self, sampled_kl):
        # Update the current KL value based on the recently measured value.
        if sampled_kl > 2.0 * self.kl_target:
            self.kl_coeff *= 1.5
        elif sampled_kl < 0.5 * self.kl_target:
            self.kl_coeff *= 0.5
        # Return the current KL value.
        return self.kl_coeff


class ValueNetworkMixin:
    """Assigns the `_value()` method to the PPOPolicy.
    This way, Policy can call `_value()` to get the current VF estimate on a
    single(!) observation (as done in `postprocess_trajectory_fn`).
    Note: When doing this, an actual forward pass is being performed.
    This is different from only calling `model.value_function()`, where
    the result of the most recent forward pass is being used to return an
    already calculated tensor.
    """

    def __init__(self, obs_space, action_space, config):
        # When doing GAE, we need the value function estimate on the
        # observation.
        if config["use_gae"]:
            # Input dict is provided to us automatically via the Model's
            # requirements. It's a single-timestep (last one in trajectory)
            # input_dict.
            if config["_use_trajectory_view_api"]:

                def value(**input_dict):
                    model_out, _ = self.model.from_batch(
                        convert_to_torch_tensor(input_dict, self.device),
                        is_training=False)
                    # [0] = remove the batch dim.
                    return self.model.value_function()[0]

            # TODO: (sven) Remove once trajectory view API is all-algo default.
            else:

                def value(ob, prev_action, prev_reward, *state):
                    model_out, _ = self.model({
                        SampleBatch.CUR_OBS: convert_to_torch_tensor(
                            np.asarray([ob]), self.device),
                        SampleBatch.PREV_ACTIONS: convert_to_torch_tensor(
                            np.asarray([prev_action]), self.device),
                        SampleBatch.PREV_REWARDS: convert_to_torch_tensor(
                            np.asarray([prev_reward]), self.device),
                        "is_training": False,
                    }, [
                        convert_to_torch_tensor(np.asarray([s]), self.device)
                        for s in state
                    ], convert_to_torch_tensor(np.asarray([1]), self.device))
                    # [0] = remove the batch dim.
                    return self.model.value_function()[0]

        # When not doing GAE, we do not require the value function's output.
        else:

            def value(*args, **kwargs):
                return 0.0

        self._value = value


def setup_mixins(policy: Policy, obs_space: gym.spaces.Space,
                 action_space: gym.spaces.Space,
                 config: TrainerConfigDict) -> None:
    """Call all mixin classes' constructors before PPOPolicy initialization.
    Args:
        policy (Policy): The Policy object.
        obs_space (gym.spaces.Space): The Policy's observation space.
        action_space (gym.spaces.Space): The Policy's action space.
        config (TrainerConfigDict): The Policy's config.
    """
    ValueNetworkMixin.__init__(policy, obs_space, action_space, config)
    KLCoeffMixin.__init__(policy, config)
    EntropyCoeffSchedule.__init__(policy, config["entropy_coeff"],
                                  config["entropy_coeff_schedule"])
    LearningRateSchedule.__init__(policy, config["lr"], config["lr_schedule"])


# Build a child class of `TorchPolicy`, given the custom functions defined
# above.
MultiPPOTorchPolicy = build_policy_class(
    name="MultiPPOTorchPolicy",
    framework="torch",
    get_default_config=lambda: ray.rllib.agents.ppo.ppo.DEFAULT_CONFIG,
    loss_fn=ppo_surrogate_loss,
    stats_fn=kl_and_loss_stats,
    extra_action_out_fn=vf_preds_fetches,
    postprocess_fn=compute_gae_for_sample_batch,
    extra_grad_process_fn=apply_grad_clipping,
    before_init=setup_config,
    before_loss_init=setup_mixins,
    mixins=[
        LearningRateSchedule, EntropyCoeffSchedule, KLCoeffMixin,
        ValueNetworkMixin
    ],
)

def get_policy_class(config):
    return MultiPPOTorchPolicy

MultiPPOTrainer = build_trainer(
    name="MultiPPO",
    default_config=ray.rllib.agents.ppo.ppo.DEFAULT_CONFIG,
    validate_config=ray.rllib.agents.ppo.ppo.validate_config,
    default_policy=MultiPPOTorchPolicy,
    get_policy_class=get_policy_class,
    execution_plan=ray.rllib.agents.ppo.ppo.execution_plan
)
