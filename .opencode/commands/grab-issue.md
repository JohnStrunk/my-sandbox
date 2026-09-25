---
description: Take the next issue from the backlog and start working on it.
agent: build
---

# Task: Start working on the next issue from the backlog

- Use a subagent to examine the open, unassigned issues in the backlog, and
  have it select an unblocked, high-value issue that is currently unassigned.
- Assign the selected issue to yourself.
- Begin working on the issue by reviewing the requirements and any related
  documentation.
- Create a plan for how to approach the issue, including any necessary steps
  or tasks.
- Update the issue with your plan and any initial findings or questions.
- Using a local worktree, implement the fix or feature as outlined in your
  plan.
- Test your changes thoroughly to ensure they meet the requirements and do not
  introduce new issues.
- Have a subagent review your changes for quality and completeness.
- Once the review is complete, address any feedback provided by the subagent.
- After making any necessary adjustments, repeat the subagent review process
  up to 3 times if needed.
- Make a pull request.
- Ensure that CI passed and that all tests are successful.
- Wait for the pull request to merge.
- Update the local main branch.
- Remove the worktree and local branch used for the issue.
- Provide a summary of the work completed:
  - Include a brief description of the issue and the solution implemented.
  - Highlight any challenges faced and how they were overcome.
