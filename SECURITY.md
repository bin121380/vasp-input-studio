# Security Policy

## Supported versions

VASP Input Studio is currently an early public beta. Security fixes are applied to the latest code on the `main` branch and to the latest published release.

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| `main` | Yes |
| Older releases | No |

## Reporting a vulnerability

Please do not disclose suspected vulnerabilities in a public issue, discussion, or pull request.

Use GitHub's private vulnerability reporting flow from the repository's **Security** tab. If that option is unavailable, contact the maintainer through their GitHub profile and request a private reporting channel before sharing technical details.

Include, when possible:

- the affected version or commit;
- the operating system and Python version;
- clear reproduction steps;
- the expected and observed behavior;
- the potential impact;
- any suggested mitigation.

Please remove VASP license files, POTCAR data, credentials, tokens, private calculation inputs, and personal information from reports and attachments.

The maintainer will acknowledge a valid report, assess severity, and coordinate a fix and disclosure timeline. Please allow reasonable time for remediation before public disclosure.

## Scope notes

VASP Input Studio is intended for single-user use on a trusted workstation or small cluster. It has no built-in authentication and can execute user-configured commands and manage files inside its configured workspace. Binding the service to an untrusted network or the public internet is outside its supported threat model.
