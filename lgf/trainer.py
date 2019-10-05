import os
from collections import Counter

import numpy as np

import torch

from ignite.engine import Events, Engine
from ignite.exceptions import NotComputableError
from ignite.metrics import RunningAverage, Metric, Loss
from ignite.handlers import ModelCheckpoint, EarlyStopping, TerminateOnNan
from ignite.contrib.handlers.tqdm_logger import ProgressBar
from ignite.contrib.handlers.tensorboard_logger import TensorboardLogger, OutputHandler, GradsScalarHandler


class AverageMetric(Metric):
    def reset(self):
        self._sums = Counter()
        self._num_examples = Counter()

    def update(self, output):
        for k, v in output.items():
            self._sums[k] += torch.sum(v)
            self._num_examples[k] += torch.numel(v)

    def compute(self):
        return {k: v / self._num_examples[k] for k, v in self._sums.items()}

    def completed(self, engine):
        engine.state.metrics = {**engine.state.metrics, **self.compute()}

    def attach(self, engine):
        engine.add_event_handler(Events.EPOCH_STARTED, self.started)
        engine.add_event_handler(Events.ITERATION_COMPLETED, self.iteration_completed)
        engine.add_event_handler(Events.ITERATION_COMPLETED, self.completed)


class Trainer:
    _STEPS_PER_LOSS_WRITE = 10
    _STEPS_PER_GRAD_WRITE = 10

    def __init__(
            self,
            module,
            train_loss,
            valid_loss,
            test_metrics,
            train_loader,
            valid_loader,
            test_loader,
            opt,
            max_bad_valid_epochs,
            visualizer,
            writer,
            max_epochs,
            epochs_per_test,
            should_save_checkpoints,
            epochs_per_checkpoint,
            device
    ):
        self._module = module
        self._module.to(device)

        self._train_loss = train_loss
        self._valid_loss = valid_loss
        self._test_metrics = test_metrics

        self._train_loader = train_loader
        self._valid_loader = valid_loader
        self._test_loader = test_loader

        self._opt = opt

        self._max_epochs = max_epochs
        self._max_bad_valid_epochs = max_bad_valid_epochs
        self._best_valid_loss = float("inf")
        self._num_bad_valid_epochs = 0
        self._epochs_per_test = epochs_per_test
        self._epochs_per_checkpoint = epochs_per_checkpoint
        self._should_save_checkpoints = should_save_checkpoints

        self._visualizer = visualizer

        self._writer = writer

        self._device = device

        self._trainer = Engine(self._train_batch)
        self._trainer.add_event_handler(Events.EPOCH_STARTED, self._checkpoint)
        self._trainer.add_event_handler(Events.EPOCH_STARTED, lambda _: self._module.train())
        self._trainer.add_event_handler(Events.ITERATION_COMPLETED, TerminateOnNan())
        self._trainer.add_event_handler(Events.ITERATION_COMPLETED, self._log_training_info)
        self._trainer.add_event_handler(Events.EPOCH_COMPLETED, self._validate)
        self._trainer.add_event_handler(Events.EPOCH_COMPLETED, self._test)
        AverageMetric().attach(self._trainer)
        ProgressBar(persist=False).attach(self._trainer, ["loss"])

        self._evaluator = Engine(self._validate_batch)
        self._evaluator.add_event_handler(Events.EPOCH_STARTED, lambda _: self._module.eval())
        AverageMetric().attach(self._evaluator)
        ProgressBar(persist=False, desc="Validating").attach(self._evaluator)

        self._tester = Engine(self._test_batch)
        self._tester.add_event_handler(Events.EPOCH_STARTED, lambda _: self._module.eval())
        AverageMetric().attach(self._tester)
        ProgressBar(persist=False, desc="Testing").attach(self._tester)

    def train(self):
        self._trainer.run(data=self._train_loader, max_epochs=self._max_epochs)

    def _train_batch(self, engine, batch):
        x, _ = batch # TODO: Potentially pass y also for genericity
        x = x.to(self._device)

        self._opt.zero_grad()
        loss = self._train_loss(self._module, x).mean()
        loss.backward()
        self._opt.step()

        return {"loss": loss}

    @torch.no_grad()
    def _test(self, engine):
        epoch = engine.state.epoch
        if (epoch - 1) % self._epochs_per_test == 0: # Test after first epoch
            state = self._tester.run(data=self._test_loader)

            for k, v in state.metrics.items():
                self._writer.write_scalar(f"test/{k}", v, global_step=engine.state.epoch)

            self._visualizer.visualize(self._module, epoch)

    def _test_batch(self, engine, batch):
        x, _ = batch
        x = x.to(self._device)
        return self._test_metrics(self._module, x)

    @torch.no_grad()
    def _validate(self, engine):
        state = self._evaluator.run(data=self._valid_loader)
        valid_loss = state.metrics["loss"]

        if valid_loss < self._best_valid_loss:
            print(f"\nBest validation loss {valid_loss} after epoch {engine.state.epoch}")
            self._num_bad_valid_epochs = 0
            self._best_valid_loss = valid_loss
            self._save_checkpoint(tag="best_valid")

        else:
            self._num_bad_valid_epochs += 1

            if self._num_bad_valid_epochs > self._max_bad_valid_epochs:
                print(f"\nNo validation improvement after {self._num_bad_valid_epochs} epochs. Terminating.")
                self._trainer.terminate()

    def _validate_batch(self, engine, batch):
        x, _ = batch
        x = x.to(self._device)
        return {"loss": self._valid_loss(self._module, x)}

    def _log_training_info(self, engine):
        i = engine.state.iteration

        if i % self._STEPS_PER_LOSS_WRITE == 0:
            loss = engine.state.output["loss"]
            self._writer.write_scalar("train/loss", loss, global_step=i)

        if i % self._STEPS_PER_GRAD_WRITE == 0:
            self._writer.write_scalar("train/grad-norm", self._grad_norm(), global_step=i)

    def _grad_norm(self):
        norm = 0
        for param in self._module.parameters():
            if param.grad is not None:
                norm += param.grad.norm().item()**2
        return np.sqrt(norm)

    def _checkpoint(self, engine):
        epoch = engine.state.epoch
        if self._should_save_checkpoints and (epoch - 1) % self._epochs_per_checkpoint == 0:
            self._save_checkpoint(tag=f"before_epoch_{epoch:09}")

    def _save_checkpoint(self, tag):
        checkpoint = {
            "epoch": self._trainer.state.epoch,
            "iteration": self._trainer.state.iteration,
            "module_state_dict": self._module.state_dict(),
            "opt_state_dict": self._opt.state_dict(),
            "best_valid_loss": self._best_valid_loss
        }

        self._writer.write_checkpoint(tag, checkpoint)
