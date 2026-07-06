"""Listwise game-ball selector: which of a frame's K candidates is the game ball, or none.

Small shared MLP phi scores each candidate from CONTEXT features
(:mod:`training.world_model.selector_features` — no appearance, no track history);
mean+max pooling over phi embeddings gives a frame context vector that produces the
"none visible" logit. Softmax over K+1 -> calibrated P(candidate j is the game ball)
and P(no visible ball) — the Viterbi emission (``-log p``) and the per-frame miss cost
(``-log p_none``). ~10k params: trains in minutes on CPU, scores a full game in seconds.

torch is imported lazily so dump-only tooling can run in torch-less environments.
"""

from __future__ import annotations

import numpy as np


def _torch():
    import torch  # noqa: PLC0415
    from torch import nn  # noqa: PLC0415

    return torch, nn


def build_selector_net(n_features: int, hidden: int = 64, emb: int = 32):
    """Construct the listwise net. Returns a ``torch.nn.Module`` with
    ``forward(feats (B, K, F), mask (B, K)) -> logits (B, K+1)`` (last = none)."""
    torch, nn = _torch()

    class SelectorNet(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.phi = nn.Sequential(
                nn.Linear(n_features, hidden),
                nn.ReLU(),
                nn.Linear(hidden, hidden),
                nn.ReLU(),
                nn.Linear(hidden, emb),
                nn.ReLU(),
            )
            self.head = nn.Linear(emb, 1)
            self.none_head = nn.Linear(2 * emb, 1)
            self.temperature = nn.Parameter(torch.ones(1), requires_grad=False)

        def forward(self, feats, mask):
            e = self.phi(feats)  # (B, K, emb)
            m = mask.unsqueeze(-1)
            e = e * m
            cand = self.head(e).squeeze(-1)  # (B, K)
            cand = cand.masked_fill(~mask, float("-inf"))
            denom = m.sum(dim=1).clamp(min=1.0)
            mean = e.sum(dim=1) / denom
            mx = e.masked_fill(~m.bool(), float("-inf")).amax(dim=1)
            mx = torch.where(torch.isfinite(mx), mx, torch.zeros_like(mx))
            none = self.none_head(torch.cat([mean, mx], dim=-1))  # (B, 1)
            return torch.cat([cand, none], dim=1) / self.temperature

    return SelectorNet()


def pack_frames(
    feats_list: list[np.ndarray], top_k: int = 24
) -> tuple[np.ndarray, np.ndarray]:
    """Pad per-frame ``(K_t, F)`` features to ``(N, top_k, F)`` + bool mask ``(N, top_k)``."""
    n, f = len(feats_list), feats_list[0].shape[1] if feats_list else 0
    feats = np.zeros((n, top_k, f), np.float32)
    mask = np.zeros((n, top_k), bool)
    for i, x in enumerate(feats_list):
        k = min(len(x), top_k)
        feats[i, :k] = x[:k]
        mask[i, :k] = True
    return feats, mask


def train_selector(
    feats: np.ndarray,
    mask: np.ndarray,
    labels: np.ndarray,
    weights: np.ndarray,
    *,
    val_frac: float = 0.15,
    epochs: int = 60,
    lr: float = 1e-3,
    weight_decay: float = 1e-4,
    batch: int = 512,
    patience: int = 8,
    seed: int = 0,
):
    """Train on packed frames. ``labels`` = candidate index or K (=none). Returns
    ``(net, history)``; the best-val-loss state is restored and temperature-calibrated
    on the val split (so ``softmax(logits)`` is a usable probability)."""
    torch, _nn = _torch()
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    n = len(feats)
    idx = rng.permutation(n)
    n_val = max(1, int(n * val_frac))
    vi, ti = idx[:n_val], idx[n_val:]

    net = build_selector_net(feats.shape[2])
    opt = torch.optim.AdamW(net.parameters(), lr=lr, weight_decay=weight_decay)
    tf = torch.as_tensor(feats)
    tm = torch.as_tensor(mask)
    tl = torch.as_tensor(labels, dtype=torch.long)
    tw = torch.as_tensor(weights, dtype=torch.float32)

    def _loss(sub):
        logits = net(tf[sub], tm[sub])
        ce = torch.nn.functional.cross_entropy(logits, tl[sub], reduction="none")
        return (ce * tw[sub]).sum() / tw[sub].sum()

    best, best_state, bad, history = np.inf, None, 0, []
    for ep in range(epochs):
        net.train()
        perm = rng.permutation(len(ti))
        for s in range(0, len(ti), batch):
            sub = torch.as_tensor(ti[perm[s : s + batch]])
            opt.zero_grad()
            loss = _loss(sub)
            loss.backward()
            opt.step()
        net.eval()
        with torch.no_grad():
            vl = float(_loss(torch.as_tensor(vi)))
        history.append(vl)
        if vl < best - 1e-4:
            best, bad = vl, 0
            best_state = {k: v.clone() for k, v in net.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                break
    if best_state is not None:
        net.load_state_dict(best_state)

    # temperature calibration on the val split (simple 1-D search)
    net.eval()
    with torch.no_grad():
        logits = net(tf[vi], tm[vi])
        ce = torch.nn.functional.cross_entropy
        temps = torch.linspace(0.5, 4.0, 36)
        losses = [float(ce(logits / t, tl[vi])) for t in temps]
        net.temperature.fill_(float(temps[int(np.argmin(losses))]))
    return net, {"val_loss": history, "best": best, "epochs_run": len(history)}


def predict_probs(net, feats: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """``(N, K+1)`` calibrated probabilities (last column = none visible)."""
    torch, _nn = _torch()
    net.eval()
    with torch.no_grad():
        logits = net(torch.as_tensor(feats), torch.as_tensor(mask))
        return torch.softmax(logits, dim=1).numpy()


def save_selector(net, keep: np.ndarray, path) -> None:
    """Persist a trained selector + its feature keep-mask (full-game replay needs
    BOTH — the mask defines which FEATURE_NAMES columns the net was trained on)."""
    torch, _nn = _torch()
    torch.save(
        {
            "schema": "selector_net/1",
            "state": net.state_dict(),
            "keep": np.asarray(keep, bool),
        },
        path,
    )


def load_selector(path):
    """Load a :func:`save_selector` file -> ``(net, keep_mask)``."""
    torch, _nn = _torch()
    d = torch.load(path, map_location="cpu", weights_only=False)
    keep = np.asarray(d["keep"], bool)
    net = build_selector_net(int(keep.sum()))
    net.load_state_dict(d["state"])
    return net, keep
