from typing import Any, Callable, Iterable, Tuple

import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn as nn

import src.utils as ut
from src.data import P
from src.dsl import NotearsMLP, NotearsSobolev


class NOTEARS(nn.Module):
    def __init__(
        self,
        dim: int,  # Dims of system
        nonlinear_dims: list = [10, 10, 1],  # Dims for non-linear arch
        sem_type: str = "mlp",
        rho: float = 1.0,  # NOTEARS parameters
        alpha: float = 1.0,  # |
        lambda1: float = 0.0,  # |
        lambda2: float = 0.0,  # |
    ):
        super().__init__()

        self.dim = dim
        self.notears = (
            NotearsMLP(dims=[dim, *nonlinear_dims])
            if sem_type == "mlp"
            else NotearsSobolev(dim, 5)
        )

        self.rho = rho
        self.alpha = alpha
        self.lambda1 = lambda1
        self.lambda2 = lambda2

    def _squared_loss(self, x, x_hat):
        n = x.shape[0]
        return 0.5 / n * torch.sum((x_hat - x) ** 2)

    def h_func(self):
        return self.notears.h_func()

    def loss(self, x, x_hat):
        loss = self._squared_loss(x, x_hat)
        h_val = self.notears.h_func()
        penalty = 0.5 * self.rho * h_val * h_val + self.alpha * h_val
        l2_reg = 0.5 * self.lambda2 * self.notears.l2_reg()
        l1_reg = self.lambda1 * self.notears.fc1_l1_reg()

        return loss + penalty + l2_reg + l1_reg

    def forward(self, x: torch.Tensor):
        x_hat = self.notears(x)
        loss = self.loss(x, x_hat)

        return x_hat, loss


class lit_NOTEARS(pl.LightningModule):
    def __init__(
        self,
        model: NOTEARS,
        h_tol: float = 1e-8,
        rho_max: float = 1e16,
        w_threshold: float = 0.3,
        n: int = 200,
        s: int = 9,
        K: int = 5,
        dag_type="ER",
        dim: int = 5,
        save_hyperparams: bool = True,
    ):
        super().__init__()

        self.model = model
        self.h = np.inf

        self.h_tol, self.rho_max = h_tol, rho_max
        self.w_threshold = w_threshold

        # We need a way to cope with NOTEARS dual
        #   ascent strategy.
        self.automatic_optimization = False

        if save_hyperparams:
            self.save_hyperparameters(ignore=["model"])

        if dag_type == "ER":
            dag = 1
        elif dag_type == "SF":
            dag = 2
        elif dag_type == "BP":
            dag = 3

        self.log_dict({"s": s, "dag_type": dag, "dim": dim, "n": n, "K": K})

    def _dual_ascent_step(self, x, optimizer: torch.optim.Optimizer) -> Tuple[float]:
        h_new = None

        while self.model.rho < self.rho_max:

            def closure():
                optimizer.zero_grad()
                _, loss = self.model(x)
                self.manual_backward(loss)
                return loss

            optimizer.step(closure)

            with torch.no_grad():
                h_new = self.model.h_func().item()
            if h_new > 0.25 * self.h:
                self.model.rho *= 10
            else:
                break
        self.model.alpha += self.model.rho * h_new
        return self.model.alpha, self.model.rho, h_new

    def training_step(self, batch, batch_idx) -> Any:
        opt = self.optimizers()

        (X,) = batch

        alpha, rho, h = self._dual_ascent_step(X, opt)
        self.h = h

        self.log("h", h, on_step=True, logger=True, prog_bar=True)
        self.log("rho", rho, on_step=True, logger=True, prog_bar=True)
        self.log("alpha", alpha, on_step=True, logger=True)

    def configure_optimizers(self) -> torch.optim.Optimizer:
        return ut.LBFGSBScipy(self.model.parameters())

    def A(self, grad: bool = False) -> np.ndarray:
        if grad:
            B_est = self.model.notears.fc1_to_adj_grad()

        else:
            B_est = self.model.notears.fc1_to_adj()
            B_est[np.abs(B_est) < self.w_threshold] = 0
            B_est[B_est > 0] = 1

        return B_est

    def test_step(self, batch, batch_idx) -> Any:
        B_est = self.A()
        B_true = self.trainer.datamodule.DAG

        self.log_dict(ut.count_accuracy(B_true, B_est))


class DStruct(pl.LightningModule):
    def __init__(
        self,
        dim: int,
        dsl: Callable,
        dsl_config: dict,
        p: P,
        K: int = 5,
        lr: float = 0.001,
        lmbda: int = 2,
        n: int = 200,
        s: int = 9,
        dag_type="ER",
        h_tol: float = 1e-8,
        rho_max: float = 1e16,
        w_threshold: float = 0.3,
    ):
        super().__init__()

        self.h_tol, self.rho_max, self.w_threshold = h_tol, rho_max, w_threshold

        self.lr = lr
        self.K = K
        self.dim = dim
        self.lmbda = lmbda
        self.s = s

        self.automatic_optimization = False
        self.dsl_list = nn.ModuleList([NOTEARS(dim=self.dim) for i in range(self.K)])

        for i, dsl in enumerate(self.dsl_list):
            self.dsl_list[i].h = np.inf

        self.p = p

        self.save_hyperparameters()

    def training_step(self, batch, batch_idx):
        (X,) = batch
        subsets = self.p(X)

        opts = self.optimizers()
        opt = opts[0]
        opt.zero_grad()

        if self.current_epoch >= 0:
            hs, rhos, alphas = [], [], []
            for i, dsl in enumerate(self.dsl_list):
                subset = subsets[i]

                alpha, rho, h = self._dual_ascent_step(subset, opts[i + 1], dsl)
                dsl.h = h

                hs.append(h)
                rhos.append(rho)
                alphas.append(alpha)

            hs, rhos, alphas = np.array(hs), np.array(rhos), np.array(alphas)
            h, rho, alpha = hs.max(), rhos.min(), alphas.mean()

            self.log_dict({"h": h, "rho": rho, "alpha": alpha})

        # def closure():
        #     opt.zero_grad()
        #     loss = self._loss()
        #     self.manual_backward(loss)
        #     print(f"Loss: {loss.item()}")
        #     self.log('training loss', loss.item())

        #     return loss

        # print(batch_idx)
        # opt.step(closure)

    def _dual_ascent_step(
        self, x, optimizer: torch.optim.Optimizer, dsl: NOTEARS
    ) -> Tuple[float]:
        h_new = None

        while dsl.rho < self.rho_max:

            def closure():
                optimizer.zero_grad()
                mse_loss = self._loss()
                self.log(
                    "mse_loss",
                    mse_loss.item(),
                    on_step=True,
                    logger=True,
                    prog_bar=True,
                )
                _, loss = dsl(x)

                self.log(
                    "dsl_loss", loss.item(), on_step=True, logger=True, prog_bar=True
                )
                loss += self.lmbda * mse_loss.item()
                self.log(
                    "total_loss", loss.item(), on_step=True, logger=True, prog_bar=True
                )

                self.manual_backward(loss)
                return loss

            optimizer.step(closure)

            with torch.no_grad():
                h_new = dsl.h_func().item()
            if h_new > 0.25 * dsl.h:
                dsl.rho *= 10
            else:
                break

        dsl.alpha += dsl.rho * h_new

        return dsl.alpha, dsl.rho, h_new

    def configure_optimizers(self) -> Iterable[torch.optim.Optimizer]:
        dsl_optimizers = [ut.LBFGSBScipy(dsl.parameters()) for dsl in self.dsl_list]

        self_optim = ut.LBFGSBScipy(self.dsl_list.parameters())

        return tuple([self_optim, *dsl_optimizers])

    def forward(self, threshold=0.5, grad: bool = True):
        if grad:
            As = tuple([dsl.notears.fc1_to_adj_grad() for dsl in self.dsl_list])
            _As = torch.stack(As).mean(dim=0)
        else:
            As = np.array([dsl.notears.fc1_to_adj() for dsl in self.dsl_list])

            _As = As.mean(axis=0)

            _As[np.abs(_As) > threshold] = 1
            _As[np.abs(_As) <= threshold] = 0

        return As, _As

    def _loss(self):
        As, A_comp = self.forward()

        mask = torch.ones(A_comp.shape)
        mask.diagonal().zero_()

        A_comp.detach()
        A_comp.diagonal().zero_()

        loss = 0
        mse = nn.MSELoss()
        for A_est in As:
            loss += mse(A_est * mask, A_comp)
        return loss

    def A(self, threshold=0.5) -> np.ndarray:
        _, A = self.forward(threshold=threshold, grad=False)
        return A

    def test_step(self, batch, batch_idx) -> Any:
        for threshold in np.linspace(start=0, stop=1, num=100):
            B_est = self.A(threshold)
            if ut.is_dag(B_est):
                print(f"Is DAG for {threshold}")
                self.log_dict({"DAG_threshold": threshold})
                break

        B_true = self.trainer.datamodule.DAG
        print(f"B_est: {B_est}")
        print(f"B_true: {B_true}")
        self.log_dict(ut.count_accuracy(B_true, B_est))
