# Contributing

[中文](CONTRIBUTING.md)

Use a focused branch and include tests or evidence proportional to the change.

```bash
python -m venv .venv
.venv/bin/python -m pip install --editable '.[dev]'
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Changes to state, event, checkpoint, claim, Skill, or evidence contracts must
remain replayable and versioned. Pull requests should explain the failure mode,
the authority boundary, the verification performed, and compatibility impact.

Use Conventional Commits. Do not include generated agent attribution, raw
conversation exports, credentials, personal filesystem paths, or private
project identifiers.

## Contributions

By submitting a contribution for inclusion, you agree that it is provided under
Apache-2.0. Add a Developer Certificate of Origin sign-off to commits:

```text
Signed-off-by: Your Name <your-public-email@example.com>
```

The project name is Continuity Plane. Independent integrations may use their
own names and must not imply that they are official releases.
