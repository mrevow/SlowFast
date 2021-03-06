#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.

"""Train a video classification model."""
import os
import numpy as np
import pprint
import torch
import time
from fvcore.nn.precise_bn import get_bn_modules, update_bn_stats
import matplotlib.pyplot as plt
import torch.distributed as dist

import slowfast.models.losses as losses
import slowfast.models.optimizer as optim
import slowfast.utils.checkpoint as cu
import slowfast.utils.distributed as du
import slowfast.utils.metrics as metrics
import slowfast.utils.misc as misc
import slowfast.visualization.tensorboard_vis as tb
from slowfast.datasets import loader
from slowfast.models import build_model
from slowfast.utils.meters import AVAMeter, TrainMeter, ValMeter
from slowfast.utils.multigrid import MultigridSchedule

from common.utils.logger import CreateLogger

class Trainer(object):
    def __init__(self, cfg):
        self.logger = None
        self.cfg = cfg

    def plotStats(self, stats, ite, typ):
        if du.is_master_proc() and stats is not None:
            for k, v in stats.items():
                try:
                    val = float(v)
                    nme = "{}_{}".format(typ, k)
                    maxx = self.cfg.METRICS.PLOT_MAX_LIMITS.get(k, None)
                    val = min(val, maxx) if maxx is not None else val
                    minn = self.cfg.METRICS.PLOT_MIN_LIMITS.get(k, None)
                    val = max(val, minn) if minn is not None else val
                    self.logger.log_row(name=nme, iter=ite, val=val, description="{} master proc".format(nme))
                except ValueError:
                    pass

    def train_epoch(
        self, train_loader, model, optimizer, train_meter, cur_epoch, cfg, writer=None
    ):
        """
        Perform the video training for one epoch.
        Args:
            train_loader (loader): video training loader.
            model (model): the video model to train.
            optimizer (optim): the optimizer to perform optimization on the model's
                parameters.
            train_meter (TrainMeter): training meters to log the training performance.
            cur_epoch (int): current epoch of training.
            cfg (CfgNode): configs. Details can be found in
                slowfast/config/defaults.py
            writer (TensorboardWriter, optional): TensorboardWriter object
                to writer Tensorboard log.
        """
        # Enable train mode.
        model.train()
        train_meter.iter_tic()
        data_size = len(train_loader)
        start = time.time()
        btch = cfg.TRAIN.BATCH_SIZE * self.cfg.NUM_SHARDS
        rankE = os.environ.get("RANK", None)
        worldE = os.environ.get("WORLD_SIZE", None)
        dSize = data_size * btch
        self.logger.info("Train Epoch {} dLen {} Batch {} dSize {} localRank {} rank {} {} world {} {}".format(
            cur_epoch, data_size, btch, dSize, du.get_local_rank(), du.get_rank(), rankE, du.get_world_size(), worldE))
        tot = 0
        first = True
        predsAll = []
        labelsAll = []

        for cur_iter, (inputs, labels, _, meta) in enumerate(train_loader):
            # Transfer the data to the current GPU device.
            tot += len(labels)
            if isinstance(inputs, (list,)):
                if first:
                    self.logger.info("rank {} LEN {}  {} shape Slow {} Fast {} {} tot {}".format(du.get_rank(), len(labels), len(inputs), 
                        inputs[0].shape, inputs[1].shape, labels[0].shape, tot))
                    first = False
                for i in range(len(inputs)):
                    inputs[i] = inputs[i].cuda(non_blocking=True)
            else:
                if first:
                    self.logger.info("rank {} LEN {} shape {} {} tot {}".format(du.get_rank(), len(labels),  
                        inputs.shape, labels[0].shape, tot))
                    first = False
                inputs = inputs.cuda(non_blocking=True)
            labels = labels.cuda()
            
            for key, val in meta.items():
                if isinstance(val, (list,)):
                    for i in range(len(val)):
                        val[i] = val[i].cuda(non_blocking=True)
                else:
                    meta[key] = val.cuda(non_blocking=True)

            # Update the learning rate.
            lr = optim.get_epoch_lr(cur_epoch + float(cur_iter) / data_size, cfg)
            optim.set_lr(optimizer, lr)
            if cfg.DETECTION.ENABLE:
                # Compute the predictions.
                preds = model(inputs, meta["boxes"])

            else:
                # Perform the forward pass.
                preds = model(inputs)
            # Explicitly declare reduction to mean.
            loss_fun = losses.get_loss_func(cfg.MODEL.LOSS_FUNC)(reduction="mean")

            # Compute the loss.
            loss = loss_fun(preds, labels)

            # check Nan Loss.
            misc.check_nan_losses(loss)

            # Perform the backward pass.
            optimizer.zero_grad()
            loss.backward()
            # Update the parameters.
            optimizer.step()

            if cfg.DETECTION.ENABLE:
                if cfg.NUM_GPUS > 1:
                    loss = du.all_reduce([loss])[0]
                loss = loss.item()

                train_meter.iter_toc()
                # Update and log stats.
                train_meter.update_stats(None, None, None, loss, lr)
                # write to tensorboard format if available.
                if writer is not None:
                    writer.add_scalars(
                        {"Train/loss": loss, "Train/lr": lr},
                        global_step=data_size * cur_epoch + cur_iter,
                    )
                ite = data_size * cur_epoch + cur_iter
                if du.is_master_proc():
                    self.logger.log_row(name='TrainLoss', iter=ite, loss=loss, description="train loss")
                    self.logger.log_row(name='TrainLr', iter=ite, lr=lr, description="train learn rate")

            else:
                top1_err, top5_err = None, None
                if cfg.DATA.MULTI_LABEL:
                    # Gather all the predictions across all the devices.
                    if cfg.NUM_GPUS > 1:
                        [loss] = du.all_reduce([loss])
                    loss = loss.item()
                else:
                    # Binary classifier - save preds / labels for metrics
                    if cfg.MODEL.NUM_CLASSES == 2:
                        predsAll.extend(preds.detach().cpu().numpy()[:,-1])
                        labelsAll.extend(labels.detach().cpu().numpy())
                    # Compute the errors.
                    num_topks_correct = metrics.topks_correct(preds, labels, (1, min(5, cfg.MODEL.NUM_CLASSES)))
                    top1_err, top5_err = [
                        (1.0 - x / preds.size(0)) * 100.0 for x in num_topks_correct
                    ]

                    # Gather all the predictions across all the devices.
                    if cfg.NUM_GPUS > 1:
                        loss, top1_err, top5_err = du.all_reduce(
                            [loss, top1_err, top5_err]
                        )

                    # Copy the stats from GPU to CPU (sync point).
                    loss, top1_err, top5_err = (
                        loss.item(),
                        top1_err.item(),
                        top5_err.item(),
                    )

                train_meter.iter_toc()
                # Update and log stats.
                # self.logger.info("UPDATING stat {} {} {}".format(inputs[0].size(0), cfg.NUM_GPUS, inputs[0].size(0) * cfg.NUM_GPUS))
                train_meter.update_stats(
                    top1_err, top5_err, loss, lr, inputs[0].size(0) * cfg.NUM_GPUS
                )
                # write to tensorboard format if available.
                if writer is not None:
                    writer.add_scalars(
                        {
                            "Train/loss": loss,
                            "Train/lr": lr,
                            "Train/Top1_err": top1_err,
                            "Train/Top5_err": top5_err,
                        },
                        global_step=data_size * cur_epoch + cur_iter,
                    )

            stats = train_meter.log_iter_stats(cur_epoch, cur_iter, predsAll, labelsAll)
            ite = dSize * cur_epoch + btch * (cur_iter+1)
            self.plotStats(stats, ite, 'TrainIter')
            train_meter.iter_tic()

        if du.is_master_proc() and cfg.LOG_MODEL_INFO:
            misc.log_model_info(model, cfg, use_train_input=True)
        # Log epoch stats.
        gathered = du.all_gather([torch.tensor(predsAll).to(torch.device("cuda")), torch.tensor(labelsAll).to(torch.device("cuda"))])
        stats = train_meter.log_epoch_stats(cur_epoch, gathered[0].detach().cpu().numpy(), gathered[1].detach().cpu().numpy())
        ite = (cur_epoch+1) * dSize
        self.plotStats(stats, ite, 'TrainEpoch')
        train_meter.reset()
        end = time.time()
        el = end - start
        totAll = du.all_reduce([torch.tensor(tot).cuda()], average=False)
        tSum = totAll[0].item()
        elT = torch.tensor(el).cuda()
        elMax = du.all_reduce([elT], op=dist.ReduceOp.MAX, average=False)[0].item()
        jobRate = tSum/elMax
        self.logger.info("totSampCnt {} workerSampCnt {}  eTimeMax {} eTimeWorker {}  SampPerSecJob {:.1f} SampPerSecWorker {:.1f}".format(
            tSum, tot, elMax, el, jobRate, tot/el))
        return jobRate

    @torch.no_grad()
    def eval_epoch(self, val_loader, model, val_meter, cur_epoch, cfg, writer=None):
        """
        Evaluate the model on the val set.
        Args:
            val_loader (loader): data loader to provide validation data.
            model (model): model to evaluate the performance.
            val_meter (ValMeter): meter instance to record and calculate the metrics.
            cur_epoch (int): number of the current epoch of training.
            cfg (CfgNode): configs. Details can be found in
                slowfast/config/defaults.py
            writer (TensorboardWriter, optional): TensorboardWriter object
                to writer Tensorboard log.
        """

        # Evaluation mode enabled. The running stats would not be updated.
        model.eval()
        data_size = len(val_loader)
        btch = cfg.TRAIN.BATCH_SIZE * self.cfg.NUM_SHARDS
        rankE = os.environ.get("RANK", None)
        worldE = os.environ.get("WORLD_SIZE", None)
        dSize = data_size * btch
        self.logger.info("Val Epoch {} dLen {} Batch {} dSize {} localRank {} rank {} {} world {} {}".format(
            cur_epoch, data_size, btch, dSize, du.get_local_rank(), du.get_rank(), rankE, du.get_world_size(), worldE))

        val_meter.iter_tic()
        predsAll = []
        labelsAll = []
        data_size = len(val_loader)

        for cur_iter, (inputs, labels, _, meta) in enumerate(val_loader):
            # Transferthe data to the current GPU device.
            if isinstance(inputs, (list,)):
                for i in range(len(inputs)):
                    inputs[i] = inputs[i].cuda(non_blocking=True)
            else:
                inputs = inputs.cuda(non_blocking=True)
            labels = labels.cuda()
            for key, val in meta.items():
                if isinstance(val, (list,)):
                    for i in range(len(val)):
                        val[i] = val[i].cuda(non_blocking=True)
                else:
                    meta[key] = val.cuda(non_blocking=True)

            if cfg.DETECTION.ENABLE:
                # Compute the predictions.
                preds = model(inputs, meta["boxes"])

                preds = preds.cpu()
                ori_boxes = meta["ori_boxes"].cpu()
                metadata = meta["metadata"].cpu()

                if cfg.NUM_GPUS > 1:
                    preds = torch.cat(du.all_gather_unaligned(preds), dim=0)
                    ori_boxes = torch.cat(du.all_gather_unaligned(ori_boxes), dim=0)
                    metadata = torch.cat(du.all_gather_unaligned(metadata), dim=0)

                val_meter.iter_toc()
                # Update and log stats.
                val_meter.update_stats(preds.cpu(), ori_boxes.cpu(), metadata.cpu())

            else:
                preds = model(inputs)

                if cfg.DATA.MULTI_LABEL:
                    if cfg.NUM_GPUS > 1:
                        preds, labels = du.all_gather([preds, labels])
                else:
                    if cfg.MODEL.NUM_CLASSES == 2:
                        predsAll.extend(preds.detach().cpu().numpy()[:,-1])
                        labelsAll.extend(labels.detach().cpu().numpy())

                    # Compute the errors.
                    num_topks_correct = metrics.topks_correct(preds, labels, (1, min(5, cfg.MODEL.NUM_CLASSES)))

                    # Combine the errors across the GPUs.
                    top1_err, top5_err = [
                        (1.0 - x / preds.size(0)) * 100.0 for x in num_topks_correct
                    ]
                    if cfg.NUM_GPUS > 1:
                        top1_err, top5_err = du.all_reduce([top1_err, top5_err])

                    # Copy the errors from GPU to CPU (sync point).
                    top1_err, top5_err = top1_err.item(), top5_err.item()

                    val_meter.iter_toc()
                    # Update and log stats.
                    val_meter.update_stats(
                        top1_err, top5_err, inputs[0].size(0) * cfg.NUM_GPUS
                    )
                    # write to tensorboard format if available.
                    if writer is not None:
                        writer.add_scalars(
                            {"Val/Top1_err": top1_err, "Val/Top5_err": top5_err},
                            global_step=len(val_loader) * cur_epoch + cur_iter,
                        )

                    if du.is_master_proc():
                        ite = len(val_loader) * cur_epoch + cur_iter
                        self.logger.log_row(name='ValTop1', iter=ite, lr=top1_err, description="Top 1 Err")
                        self.logger.log_row(name='ValTop5', iter=ite, lr=top5_err, description="Top 5 Err")

                val_meter.update_predictions(preds, labels)

            stats = val_meter.log_iter_stats(cur_epoch, cur_iter, predsAll, labelsAll)
            ite = dSize * cur_epoch + btch * (cur_iter+1)
            self.plotStats(stats, ite, 'ValIter')

            val_meter.iter_tic()

        # Log epoch stats.
        gathered = du.all_gather([torch.tensor(predsAll).to(torch.device("cuda")), torch.tensor(labelsAll).to(torch.device("cuda"))])
        stats = val_meter.log_epoch_stats(cur_epoch, gathered[0].detach().cpu().numpy(), gathered[1].detach().cpu().numpy())
        ite = (cur_epoch+1) * dSize
        self.plotStats(stats, ite, 'ValEpoch')

        # write to tensorboard format if available.
        if writer is not None:
            if cfg.DETECTION.ENABLE:
                writer.add_scalars(
                    {"Val/mAP": val_meter.full_map}, global_step=cur_epoch
                )
            all_preds_cpu = [
                pred.clone().detach().cpu() for pred in val_meter.all_preds
            ]
            all_labels_cpu = [
                label.clone().detach().cpu() for label in val_meter.all_labels
            ]
            # plotScatter(all_preds_cpu, all_labels_cpu, "Epoch_{}".format(cur_epoch))
            # writer.plot_eval(
            #     preds=all_preds_cpu, labels=all_labels_cpu, global_step=cur_epoch
            # )
        val_meter.reset()

    def plotScatter(self, preds, labels, prefix):
        plt.close('all')
        fig, ax = plt.subplots()
        ax.plot(preds, labels, 'o', markersize=5)
        plt.gca().set_aspect('equal', adjustable='box')
        plt.grid(True)
        plt.title('{} '.format(prefix, db_name, r_p))
        plt.ylabel('labels')
        plt.xlabel('Predictions')
        self.logger.log_image(prefix, plot=fig)

    def calculate_and_update_precise_bn(self, loader, model, num_iters=200):
        """
        Update the stats in bn layers by calculate the precise stats.
        Args:
            loader (loader): data loader to provide training data.
            model (model): model to update the bn stats.
            num_iters (int): number of iterations to compute and update the bn stats.
        """

        def _gen_loader():
            for inputs, _, _, _ in loader:
                if isinstance(inputs, (list,)):
                    for i in range(len(inputs)):
                        inputs[i] = inputs[i].cuda(non_blocking=True)
                else:
                    inputs = inputs.cuda(non_blocking=True)
                yield inputs

        # Update the bn stats.
        update_bn_stats(model, _gen_loader(), num_iters)


    def build_trainer(self, cfg):
        """
        Build training model and its associated tools, including optimizer,
        dataloaders and meters.
        Args:
            cfg (CfgNode): configs. Details can be found in
                slowfast/config/defaults.py
        Returns:
            model (nn.Module): training model.
            optimizer (Optimizer): optimizer.
            train_loader (DataLoader): training data loader.
            val_loader (DataLoader): validatoin data loader.
            precise_bn_loader (DataLoader): training data loader for computing
                precise BN.
            train_meter (TrainMeter): tool for measuring training stats.
            val_meter (ValMeter): tool for measuring validation stats.
        """
        # Build the video model and print model statistics.
        model = build_model(cfg)
        if du.is_master_proc() and cfg.LOG_MODEL_INFO:
            misc.log_model_info(model, cfg, use_train_input=True)

        # Construct the optimizer.
        optimizer = optim.construct_optimizer(model, cfg)

        # Create the video train and val loaders.
        train_loader = loader.construct_loader(cfg, "train")
        val_loader = loader.construct_loader(cfg, "val")
        precise_bn_loader = loader.construct_loader(
            cfg, "train", is_precise_bn=True
        )
        # Create meters.
        train_meter = TrainMeter(len(train_loader), cfg)
        val_meter = ValMeter(len(val_loader), cfg)

        return (
            model,
            optimizer,
            train_loader,
            val_loader,
            precise_bn_loader,
            train_meter,
            val_meter,
        )


    def trainImpl(self, cfg):
        """
        Train a video model for many epochs on train set and evaluate it on val set.
        Args:
            cfg (CfgNode): configs. Details can be found in
                slowfast/config/defaults.py
        """
        # Set up environment.
        du.init_distributed_training(cfg)
        # Set random seed from configs.
        np.random.seed(cfg.RNG_SEED)
        torch.manual_seed(cfg.RNG_SEED)

        # Setup logging format.
        # logging.setup_logging(cfg.OUTPUT_DIR)

        # Init multigrid.
        multigrid = None
        if cfg.MULTIGRID.LONG_CYCLE or cfg.MULTIGRID.SHORT_CYCLE:
            multigrid = MultigridSchedule()
            cfg = multigrid.init_multigrid(cfg)
            if cfg.MULTIGRID.LONG_CYCLE:
                cfg, _ = multigrid.update_long_cycle(cfg, cur_epoch=0)
        # Print config.
        # self.logger.info("Train with config:")
        # self.logger.info(pprint.pformat(cfg))

        # Build the video model and print model statistics.
        model = build_model(cfg)
        if du.is_master_proc() and cfg.LOG_MODEL_INFO:
            misc.log_model_info(model, cfg, use_train_input=True)

        # Construct the optimizer.
        optimizer = optim.construct_optimizer(model, cfg)

        # Load a checkpoint to resume training if applicable.
        start_epoch = cu.load_train_checkpoint(cfg, model, optimizer, self.logger)

        # Create the video train and val loaders.
        train_loader = loader.construct_loader(cfg, "train")
        self.logger.info("Train: Loaded {} labels".format(len(train_loader)))
        val_loader = loader.construct_loader(cfg, "val")
        self.logger.info("Val: Loaded {} labels".format(len(val_loader)))
        precise_bn_loader = loader.construct_loader(
            cfg, "train", is_precise_bn=True
        )

        # Create meters.
        if cfg.DETECTION.ENABLE:
            train_meter = AVAMeter(len(train_loader), cfg, mode="train")
            val_meter = AVAMeter(len(val_loader), cfg, mode="val")
        else:
            train_meter = TrainMeter(len(train_loader), cfg, self.logger)
            val_meter = ValMeter(len(val_loader), cfg, self.logger)

        # set up writer for logging to Tensorboard format.
        if cfg.TENSORBOARD.ENABLE and du.is_master_proc(
            cfg.NUM_GPUS * cfg.NUM_SHARDS
        ):
            writer = tb.TensorboardWriter(cfg)
        else:
            writer = None

        avgRate = 0
        epCnt = 0
        self.logger.info("Train startEpoch {}  maxEpoch {}".format(start_epoch, cfg.SOLVER.MAX_EPOCH))
        for cur_epoch in range(start_epoch, cfg.SOLVER.MAX_EPOCH):
            if cfg.MULTIGRID.LONG_CYCLE:
                cfg, changed = multigrid.update_long_cycle(cfg, cur_epoch)
                if changed:
                    (
                        model,
                        optimizer,
                        train_loader,
                        val_loader,
                        precise_bn_loader,
                        train_meter,
                        val_meter,
                    ) = self.build_trainer(cfg)

                    # Load checkpoint.
                    if cu.has_checkpoint(cfg.OUTPUT_DIR):
                        last_checkpoint = cu.get_last_checkpoint(cfg.OUTPUT_DIR)
                        assert "{:05d}.pyth".format(cur_epoch) in last_checkpoint
                    else:
                        last_checkpoint = cfg.TRAIN.CHECKPOINT_FILE_PATH
                    self.logger.info("Load from {}".format(last_checkpoint))
                    cu.load_checkpoint(
                        last_checkpoint, model, cfg.NUM_GPUS > 1, optimizer
                    )

            # Shuffle the dataset.
            loader.shuffle_dataset(train_loader, cur_epoch)
            # Train for one epoch.
            avgRate += self.train_epoch(
                train_loader, model, optimizer, train_meter, cur_epoch, cfg, writer
            )
            epCnt += 1

            # Compute precise BN stats.
            if cfg.BN.USE_PRECISE_STATS and len(get_bn_modules(model)) > 0:
                self.calculate_and_update_precise_bn(
                    precise_bn_loader,
                    model,
                    min(cfg.BN.NUM_BATCHES_PRECISE, len(precise_bn_loader)),
                )
            _ = misc.aggregate_sub_bn_stats(model)

            # Save a checkpoint.
            if cu.is_checkpoint_epoch(
                cfg, cur_epoch, None if multigrid is None else multigrid.schedule
            ):
                chkFile = cu.save_checkpoint(cfg.OUTPUT_DIR, model, optimizer, cur_epoch, cfg)
                self.logger.info("Created checkpont {}".format(chkFile))
            # Evaluate the model on validation set.
            if misc.is_eval_epoch(
                cfg, cur_epoch, None if multigrid is None else multigrid.schedule
            ):
                self.eval_epoch(val_loader, model, val_meter, cur_epoch, cfg, writer)

        if writer is not None:
            writer.close()

        avgRate = avgRate / epCnt if epCnt > 0 else -1
        self.logger.info("Exiting overall jobrate {:.1f}".format(avgRate))
        self.logger.log_value('jobRate', avgRate, 'Average number lips /sec')

    def reportInfo(self, cfg):
        totGpu = cfg.NUM_GPUS * cfg.NUM_SHARDS
        rank = cfg.SHARD_ID * cfg.NUM_GPUS +  cfg.SHARD_ID
        self.logger.info("Start Train gpu_per_shard {} num_shards {} totGpu {} rank {} shardId {}".format(
            cfg.NUM_GPUS, cfg.NUM_SHARDS, totGpu, rank, cfg.SHARD_ID,
        ))
        self.logger.log_value('gpu_per_shard', cfg.NUM_GPUS, 'Number gpu per shard')
        self.logger.log_value('num_shards', cfg.NUM_SHARDS, 'Number of shards')
        self.logger.log_value('tot_gpu', totGpu, 'Total gpu count')
        self.logger.log_value('rank', rank, 'Global Id ')
        self.logger.log_value('shard_id', cfg.SHARD_ID, 'Id of the compute shard')

    def train(self, cfg):
        with CreateLogger(cfg, logger_type=cfg.logger_type) as logger:
            self.logger = logger
            self.reportInfo(cfg)
            self.trainImpl(cfg)