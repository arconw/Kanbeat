from ..config_keys import HARNESS_LABELS

HARNESS = [(label, value) for value, label in HARNESS_LABELS.items()]
SESSION_HARNESS = [*HARNESS, ("Terminal", "shell")]
