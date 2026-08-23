# Fresh-context restart
- The fresh manager sees only: contract, failure capsule, diff stat. Never the transcript.
- Decide APPLY / PARTIALLY_APPLY / DISCARD; the controller saves a patch of the interrupted diff before any discard.
- The next builder receives the capsule and a required new hypothesis.
