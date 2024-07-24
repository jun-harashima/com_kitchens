import logging
from argparse import Namespace
from typing import Any, List, Optional, Type

import torch
import torch.distributed as dist
from lightning import LightningModule
from lightning.pytorch.utilities.types import _METRIC
from torchmetrics import MaxMetric, MeanMetric

from com_kitchens.models.components.metrics.retrieval_metrics import compute_scores
from com_kitchens.models.components.xclip.modeling_xclip import XCLIP
from com_kitchens.models.components.xclip.optimization import BertAdam

logger = logging.getLogger(__name__)


class XCLIPLitModule(LightningModule):
    def __init__(
        self,
        cross_config,
        clip_state_dict,
        task_config,
    ):
        super().__init__()

        self.save_hyperparameters(logger=False)

        # clip-state-dict は概ねNone
        self.model = XCLIP.from_pretrained(
            cross_model_name=self.hparams.cross_config,
            cache_dir=self.hparams.task_config.cache_dir,
            state_dict=self.hparams.clip_state_dict,
            task_config=Namespace(**self.hparams.task_config),
        )

        self.valid_step_outputs = []
        self.test_step_outputs = []

        # metric objects for calculating and averaging accuracy across batches
        # val
        self.val_m1_recall_at_1 = MeanMetric()
        self.val_m1_recall_at_5 = MeanMetric()
        self.val_m1_recall_at_10 = MeanMetric()

        self.val_m2_recall_at_1 = MeanMetric()
        self.val_m2_recall_at_5 = MeanMetric()
        self.val_m2_recall_at_10 = MeanMetric()

        # test
        self.test_feasible_recall_at_1 = MeanMetric()
        self.test_feasible_recall_at_5 = MeanMetric()
        self.test_feasible_recall_at_10 = MeanMetric()
        self.test_feasible_median = MeanMetric()

        self.test_m2_recall_at_1 = MeanMetric()
        self.test_m2_recall_at_5 = MeanMetric()
        self.test_m2_recall_at_10 = MeanMetric()
        self.test_m2_median = MeanMetric()

        self.test_n_seq = MeanMetric()
        self.test_n_vid = MeanMetric()

        # for averaging loss across batches
        self.train_loss = MeanMetric()
        self.val_loss = MeanMetric()

        # for tracking best so far validation recall@1
        # self.val_m1_recall_at_1_best = MaxMetric()

    def _gather_outputs(self, obj: Type) -> Optional[List[Type]]:
        if not self.trainer.is_global_zero:
            dist.gather_object(obj=obj, object_gather_list=None, dst=0)
            return None
        else:  # global-zero only
            list_gather_obj = [None] * self.trainer.world_size
            dist.gather_object(obj=obj, object_gather_list=list_gather_obj, dst=0)

            # return flatten
            return [output for outputs in list_gather_obj for output in outputs]

    def log_on_epoch(
        self,
        name: str,
        value: _METRIC,
        on_step=False,
        on_epoch=True,
        prog_bar=True,
        sync_dist=False,
        **kwargs
    ) -> None:
        return super().log(
            name,
            value,
            prog_bar=prog_bar,
            sync_dist=sync_dist,
            on_step=on_step,
            on_epoch=on_epoch,
            **kwargs
        )

    def on_train_start(self):
        # by default lightning executes validation step sanity checks before training starts,
        # so it's worth to make sure validation metrics don't store results from these checks
        self.val_m1_recall_at_1.reset()
        self.val_m1_recall_at_5.reset()
        self.val_m1_recall_at_10.reset()
        self.val_m2_recall_at_1.reset()
        self.val_m2_recall_at_5.reset()
        self.val_m2_recall_at_10.reset()

    def model_step(self, batch: Any):
        # input_ids, input_mask, segment_ids, video, video_mask = batch.values()
        loss = self.model.forward(**batch)
        return loss

    def training_step(self, batch: Any, batch_idx: int):
        loss = self.model_step(batch)
        self.train_loss.update(loss)
        # update and log metrics
        self.log("train/loss", self.train_loss, on_step=True, on_epoch=True, prog_bar=True)
        return loss

    def on_validation_start(self) -> None:
        self.valid_step_outputs[:] = []

    def validation_step(self, batch: Any, batch_idx: int):
        (sequence_output, seq_features), visual_output = self.model.get_sequence_visual_output(
            **batch
        )

        loss = self.model_step(batch)
        self.val_loss.update(loss)
        # update and log metrics
        self.log("val/loss", self.val_loss, on_step=True, on_epoch=True, prog_bar=True)

        # keep outputs in CPU to void OOM
        self.valid_step_outputs.append(
            {
                "sequence_output": sequence_output.data.cpu(),
                "seq_features": seq_features.data.cpu(),
                "visual_output": visual_output.data.cpu(),
                "is_query": batch["is_query"].data.cpu(),
                "is_pool": batch["is_pool"].data.cpu(),
                "recipe_id": batch["recipe_id"].data.cpu(),
                "kitchen_id": batch["kitchen_id"].data.cpu(),
                "ap_id": batch["ap_id"].data.cpu(),
                "attention_mask": batch["attention_mask"].data.cpu(),
                "video_mask": batch["video_mask"].data.cpu(),
            }
        )

    def on_validation_epoch_end(self):
        # gather_object should be executed on all nodes
        valid_step_outputs = self._gather_outputs(obj=self.valid_step_outputs)
        valid_step_outputs = self.valid_step_outputs

        # run the validation only on rank 0
        if self.trainer.is_global_zero:
            v2t_metrics, _, _ = self._compute_v2t_metrics(valid_step_outputs)

            # update metrics
            self.val_m1_recall_at_1.update(v2t_metrics["M1-R1"])
            self.val_m1_recall_at_5.update(v2t_metrics["M1-R5"])
            self.val_m1_recall_at_10.update(v2t_metrics["M1-R10"])
            self.val_m2_recall_at_1.update(v2t_metrics["M2-R1"])
            self.val_m2_recall_at_5.update(v2t_metrics["M2-R5"])
            self.val_m2_recall_at_10.update(v2t_metrics["M2-R10"])

        # required to run on every nodes to avoid locking
        self.log_on_epoch("val/M1-R@1", self.val_m1_recall_at_1)
        self.log_on_epoch("val/M1-R@5", self.val_m1_recall_at_5)
        self.log_on_epoch("val/M1-R@10", self.val_m1_recall_at_10)
        self.log_on_epoch("val/M2-R@1", self.val_m2_recall_at_1)
        self.log_on_epoch("val/M2-R@5", self.val_m2_recall_at_5)
        self.log_on_epoch("val/M2-R@10", self.val_m2_recall_at_10)


    def on_test_start(self) -> None:
        self.test_step_outputs[:] = []

    def test_step(self, batch: Any, batch_idx: int):
        (sequence_output, seq_features), visual_output = self.model.get_sequence_visual_output(
            **batch
        )

        # keep outputs in CPU to void OOM
        self.test_step_outputs.append(
            {
                "sequence_output": sequence_output.data.cpu(),
                "seq_features": seq_features.data.cpu(),
                "visual_output": visual_output.data.cpu(),
                "is_query": batch["is_query"].data.cpu(),
                "is_pool": batch["is_pool"].data.cpu(),
                "recipe_id": batch["recipe_id"].data.cpu(),
                "kitchen_id": batch["kitchen_id"].data.cpu(),
                "ap_id": batch["ap_id"].data.cpu(),
                "attention_mask": batch["attention_mask"].data.cpu(),
                "video_mask": batch["video_mask"].data.cpu(),
            }
        )

    def on_test_epoch_end(self):
        # gather_object should be executed on each node?
        test_step_outputs = self._gather_outputs(obj=self.test_step_outputs)

        # run the validation only on rank 0
        if self.trainer.is_global_zero:
            v2t_metrics, n_seq, n_vid = self._compute_v2t_metrics(test_step_outputs)

            # self.test_feasible_recall_at_1.update(v2t_metrics["FEAS-R1"])
            # self.test_feasible_recall_at_5.update(v2t_metrics["FEAS-R5"])
            # self.test_feasible_recall_at_10.update(v2t_metrics["FEAS-R10"])
            # self.test_feasible_median.update(v2t_metrics["FEAS-median"])
            self.test_m2_recall_at_1.update(v2t_metrics["M2-R1"])
            self.test_m2_recall_at_5.update(v2t_metrics["M2-R5"])
            self.test_m2_recall_at_10.update(v2t_metrics["M2-R10"])
            self.test_m2_median.update(v2t_metrics["M2-median"])
            self.test_n_seq.update(n_seq)
            self.test_n_vid.update(n_vid)

        # required to run on every nodes to avoid locking

        # self.log("test/FEAS-R@1", self.test_feasible_recall_at_1)
        # self.log("test/FEAS-R@5", self.test_feasible_recall_at_5)
        # self.log("test/FEAS-R@10", self.test_feasible_recall_at_10)
        # self.log("test/FEAS-median", self.test_feasible_median)

        self.log("test/M2-R@1", self.test_m2_recall_at_1)
        self.log("test/M2-R@5", self.test_m2_recall_at_5)
        self.log("test/M2-R@10", self.test_m2_recall_at_10)
        self.log("test/M2-median", self.test_m2_median)

        self.log("test/n_seq", self.test_n_seq)
        self.log("test/n_vid", self.test_n_vid)

    def configure_optimizers(self):
        num_train_optimization_steps = (
            (
                self.hparams.task_config.train_data_size
                + self.hparams.task_config.gradient_accumulation_steps
                - 1
            )
            / self.hparams.task_config.gradient_accumulation_steps
        ) * self.hparams.task_config.epochs
        optimizer = None

        if hasattr(self.model, "module"):
            self.model = self.model.module

        param_optimizer = list(self.model.named_parameters())
        no_decay = ["bias", "LayerNorm.bias", "LayerNorm.weight"]

        decay_param_tp = [
            (n, p) for n, p in param_optimizer if not any(nd in n for nd in no_decay)
        ]
        no_decay_param_tp = [(n, p) for n, p in param_optimizer if any(nd in n for nd in no_decay)]

        decay_clip_param_tp = [(n, p) for n, p in decay_param_tp if "clip." in n]
        decay_noclip_param_tp = [(n, p) for n, p in decay_param_tp if "clip." not in n]

        no_decay_clip_param_tp = [(n, p) for n, p in no_decay_param_tp if "clip." in n]
        no_decay_noclip_param_tp = [(n, p) for n, p in no_decay_param_tp if "clip." not in n]

        weight_decay = 0.2
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in decay_clip_param_tp],
                "weight_decay": weight_decay,
                "lr": self.hparams.task_config.lr * self.hparams.task_config.coef_lr,
            },
            {"params": [p for n, p in decay_noclip_param_tp], "weight_decay": weight_decay},
            {
                "params": [p for n, p in no_decay_clip_param_tp],
                "weight_decay": 0.0,
                "lr": self.hparams.task_config.lr * self.hparams.task_config.coef_lr,
            },
            {"params": [p for n, p in no_decay_noclip_param_tp], "weight_decay": 0.0},
        ]

        optimizer = BertAdam(
            optimizer_grouped_parameters,
            lr=self.hparams.task_config.lr,
            warmup=self.hparams.task_config.warmup_proportion,
            schedule="warmup_cosine",
            b1=0.9,
            b2=0.98,
            e=1e-6,
            t_total=num_train_optimization_steps,
            weight_decay=weight_decay,
            max_grad_norm=1.0,
        )

        return {"optimizer": optimizer}

    def _compute_v2t_metrics(
        self,
        outputs,
        device=None,
    ):
        if len(outputs) == 0:
            return

        if device is None:
            device = next(self.model.parameters()).device

        # keep outputs on CPU unitll pass them to the model
        sequence_output = torch.cat([o["sequence_output"] for o in outputs])
        seq_features = torch.cat([o["seq_features"] for o in outputs])
        visual_output = torch.cat([o["visual_output"] for o in outputs])
        attention_mask = torch.cat([o["attention_mask"] for o in outputs])
        video_mask = torch.cat([o["video_mask"] for o in outputs])
        recipe_kitchen_ap_ids = torch.stack(
            [
                torch.cat([o["recipe_id"] for o in outputs]),
                torch.cat([o["kitchen_id"] for o in outputs]),
                torch.cat([o["ap_id"] for o in outputs]),
            ]
        ).T
        is_query = torch.cat([o["is_query"] for o in outputs])
        is_pool = torch.cat([o["is_pool"] for o in outputs])

        # query
        visual_output = visual_output[is_query].to(device)
        video_mask = video_mask[is_query].to(device)

        # pool
        sequence_output = sequence_output[is_pool].to(device)
        seq_features = seq_features[is_pool].to(device)
        attention_mask = attention_mask[is_pool].to(device)

        # similarity matrix
        retrieve_logits, _ = self.model.get_similarity_logits(
            sequence_output,
            seq_features,
            visual_output,
            attention_mask,
            video_mask,
            loose_type=self.model.loose_type,
        )
        # (n_seq, n_vis) -> (n_vis, n_seq)
        retrieve_logits = retrieve_logits.T

        scores = compute_scores(
            retrieve_logits,
            recipe_kitchen_ap_ids[is_pool].to(device),
            recipe_kitchen_ap_ids[is_query].to(device),
            stage=self.hparams.task_config.stage,
        )

        return scores, sum(is_pool), sum(is_query)


if __name__ == "__main__":
    import yaml

    with open("configs/model/xclip.yaml") as yml:
        cfg = yaml.safe_load(yml)
    cross_config = cfg["cross_config"]
    clip_state_dict = cfg["clip_state_dict"]
    task_config = cfg["task_config"]
    print(cross_config)
    print(clip_state_dict)
    print(task_config)

    _ = XCLIPLitModule(cross_config, clip_state_dict, Namespace(**task_config))
