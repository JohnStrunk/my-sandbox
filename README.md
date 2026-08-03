# Project template

This is a template to serve as a starting point for new repos.

## Features

- Pre-configured CI/CD pipeline that runs pre-commit and ensures that the
  devcontainer builds successfully.
- A basic devcontainer setup
- Mergify configuration to enable merging of PRs. See
  [.github/mergify.yml](.github/mergify.yml) for details.
- Renovate configuration to keep dependencies up to date. See
  [.github/renovate.json5](.github/renovate.json5) for details.
- Pre-commit configuration to ensure code quality. See
  [.pre-commit-config.yaml](.pre-commit-config.yaml) for details.

## New project checklist

- [ ] Adjust the Mergify configuration to customize the merge conditions
  - Consider enabling [Merge
    protections](https://docs.mergify.com/merge-protections/) to enable
    `Depends-On: <PR#>`, `Merge-After: <ISO 8601>`.
  - Consider enforcing the merge queue (only allowing Mergify to merge PRs) by
    [protecting the default
    branch](https://dashboard.mergify.com/queues/deployment/enforcement).
- [ ] Add a license to the project
- [ ] Update the README with project-specific information
- [ ] Update the devcontainer configuration based on the project's language
  and dev tools
