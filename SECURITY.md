# Security policy

Vibekill handles source code, invokes local analyzers, and can connect to a model
endpoint. Security reports are taken seriously.

## Supported versions

Until 1.0, only the latest released minor version receives security fixes.

| Version | Supported |
|---|---|
| latest 0.x | yes |
| older 0.x | no |

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could expose source code,
bypass the egress guard, execute commands, read unintended repositories, or leak
credentials.

Use GitHub's **Security → Report a vulnerability** private reporting form once
the repository is published. If private vulnerability reporting is not enabled,
contact the maintainer through the private address listed in the repository
profile and ask for a secure reporting channel; do not include exploit details in
the first message.

Please include:

- affected Vibekill version and installation method;
- operating system and Python version;
- whether the CLI, API, UI, Docker, or GitHub mode is involved;
- minimal reproduction steps using synthetic code—never send proprietary source;
- expected and observed behavior;
- impact and whether the issue is already being exploited.

You should receive acknowledgement within 5 business days. A fix and disclosure
timeline will be agreed based on severity.

## Deployment guidance

- **Prefer local mode.** It blocks public socket egress except explicitly allowed
  hosts.
- **For the strongest boundary, use `docker run --network none`.** The Python
  guard cannot sandbox an arbitrary external analyzer process.
- **Keep the FastAPI service on loopback.** It has no built-in authentication and
  accepts repository paths on the server. See [docs/API.md](docs/API.md).
- **Treat reports as source code.** They may contain quoted lines, discovered
  credentials, internal paths, and model output. `.vibekill/` is gitignored, but
  CI artifact permissions and retention are your responsibility.
- **Use read-only GitHub tokens.** Fine-grained Contents and Pull requests read
  access is sufficient. Tokens are removed from analyzer subprocess environments.
- **Use a local model endpoint** if code must not leave the machine. In GitHub
  mode, the configured model host is explicitly allowlisted.

## Scope of the egress guarantee

The runtime guard patches Python socket operations and blocks non-allowlisted DNS
and connections. External analyzers receive offline flags, telemetry opt-outs,
closed proxy settings, and a scrubbed environment, but they are separate
processes. Kernel/container network isolation is the hard boundary when required.
