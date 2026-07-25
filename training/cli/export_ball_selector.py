"""Export a trained selector checkpoint (.pt) to the product's .npz format.

The product runtime has no torch: ``video_grouper.inference.ball_selector`` runs
the listwise net with plain numpy from a ``selector_net_npz/1`` file. The export
parity-checks the torch and numpy forwards on random inputs before writing.

    python -m training.cli.export_ball_selector \
      --pt G:/ballresearch/selector/selector_v5.pt \
      --out G:/ballresearch/selector/models/selector_v5.npz
"""

from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pt", required=True, help="save_selector checkpoint (.pt)")
    ap.add_argument("--out", required=True, help="output .npz (selector_net_npz/1)")
    args = ap.parse_args()

    from training.models.selector_net import export_selector_npz

    export_selector_npz(args.pt, args.out)


if __name__ == "__main__":
    main()
