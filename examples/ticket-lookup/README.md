# Ticket lookup

The extension and service that [Your first extension](../../docs/YOUR_FIRST_EXTENSION.md)
builds, step by step. Every file the tutorial shows is a file here, and
a test checks they are the same.

```
extension/          the extension: what runs on a person's machine
  manifest.json     what it is and what it may do
  pyproject.toml    its Python package
  src/acme_tickets_extension/__init__.py
                    two tools, a PostToolUse hook
  ui/index.html     its page
  ui/main.js        the page's script (a file: the page's CSP has no inline scripts)
service/
  service.py        its service: registers with igni server, ships the
                    extension, answers export and erasure
tests/
  test_example.py   both, tested on the SDKs' fakes
```

Run the tests from this directory, in an environment with both SDKs:

```bash
pytest
```
