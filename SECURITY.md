# Security policy

## Supported versions

Ghost API is pre-1.0. Only the latest release receives security fixes.

## Reporting a vulnerability

Please **don't open a public issue**. Report privately through GitHub:
[**Report a vulnerability**](https://github.com/Asongwelewis/Ghost-API/security/advisories/new).

Include what you found, how to reproduce it, and the impact you expect. You'll get an
acknowledgement within 7 days. Once the issue is confirmed, we'll agree on a fix and a
disclosure date with you, and credit you in the advisory unless you'd rather not be named.

## What's in scope

Ghost downloads third-party package archives and reads them, so these matter most:

- **Code execution:** anything that makes Ghost run code from a package it analyses. Ghost is
  designed to read packages as text only.
- **Archive handling:** path traversal, symlinks or decompression bombs in wheels, sdists or npm
  tarballs, causing writes outside Ghost's temporary folder.
- **Hooks and the GitHub Action:** command or script injection through hook payloads, file
  names, branch names or action inputs.
- **Cache poisoning:** making Ghost trust tampered cache files in ways that hide real findings
  or run code.

Wrong findings (false positives or negatives) aren't security issues. Please report them as
normal issues.
