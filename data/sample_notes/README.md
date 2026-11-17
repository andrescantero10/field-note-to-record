# Sample notes

Invented visit notes used by the tests and the demo. No real customers.

- `normal_*.txt`: ordinary notes.
- `hostile_*.txt`: notes that contain instructions, code or tricks. They are
  attacks on the app itself and are safe to read. `tests/test_injection.py`
  runs every one of them through the pipeline against a model that returns
  the worst possible answer.

`hostile_lookalike_product.txt` contains a product code whose first letter is a
Cyrillic letter that looks like a Latin A. That is deliberate.
