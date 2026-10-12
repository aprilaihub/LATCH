"""Re-use the original WP015 analysis with caller-selected external inputs."""
from .wp015_analysis import parser, run


def main():
    args = parser().parse_args()
    output = args.output.resolve()
    inputs = [path.resolve(strict=True) for path in (args.scientific, args.bundle, args.development, args.references)]
    if any(output == source or output in source.parents for source in inputs):
        raise SystemExit("Output tree must not contain any input artifact")
    # Original analysis requires the full 1134 physical-cell corpus, 140
    # development references and 110 post-agent references; do not weaken it
    # to accept a different or incomplete dataset under the same label.
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
