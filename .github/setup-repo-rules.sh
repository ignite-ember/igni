#!/usr/bin/env bash
#
# Configure GitHub repository rulesets for ignite-ember/igni.
# Requires: gh CLI authenticated with admin access.
#
# Protections applied:
#   1. Main branch: require PR with review, block force push & deletion
#   2. All branches: block deletion
#   3. Release tags: only repo admins can create
#
# Usage: bash .github/setup-repo-rules.sh

set -euo pipefail

REPO="ignite-ember/igni"
OWNER="dmytrozezyk"

echo "==> Configuring rulesets for $REPO"

# 1. Protect main branch — require PR reviews, block force push & deletion
echo "  Creating 'Protect main' ruleset..."
gh api repos/"$REPO"/rulesets --method POST --input - <<EOF
{
  "name": "Protect main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["refs/heads/main"],
      "exclude": []
    }
  },
  "bypass_actors": [
    {
      "actor_id": 0,
      "actor_type": "RepositoryRole",
      "bypass_mode": "pull_request"
    }
  ],
  "rules": [
    {
      "type": "deletion"
    },
    {
      "type": "non_fast_forward"
    },
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "lint" },
          { "context": "test (3.12)" }
        ]
      }
    }
  ]
}
EOF

# 2. Protect all branches from deletion (except main, already covered)
echo "  Creating 'No branch deletion' ruleset..."
gh api repos/"$REPO"/rulesets --method POST --input - <<EOF
{
  "name": "No branch deletion",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["refs/heads/*"],
      "exclude": ["refs/heads/main"]
    }
  },
  "bypass_actors": [
    {
      "actor_id": 0,
      "actor_type": "RepositoryRole",
      "bypass_mode": "always"
    }
  ],
  "rules": [
    {
      "type": "deletion"
    }
  ]
}
EOF

# 3. Protect release tags — only owner can push v* tags
echo "  Creating 'Release tags' ruleset..."
gh api repos/"$REPO"/rulesets --method POST --input - <<EOF
{
  "name": "Release tags",
  "target": "tag",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["refs/tags/v*"],
      "exclude": []
    }
  },
  "bypass_actors": [],
  "rules": [
    {
      "type": "creation"
    },
    {
      "type": "deletion"
    }
  ]
}
EOF

echo ""
echo "Done. Rulesets created:"
echo "  - Protect main: PR required (1 review), CI must pass, no force push/delete"
echo "  - No branch deletion: all branches protected from deletion (admins can bypass)"
echo "  - Release tags: v* tags cannot be created or deleted (configure bypass for $OWNER in GitHub UI)"
echo ""
echo "Next steps:"
echo "  1. Go to https://github.com/$REPO/settings/rules"
echo "  2. Edit 'Release tags' ruleset — add yourself ($OWNER) as a bypass actor"
echo "  3. Configure the 'release' environment at https://github.com/$REPO/settings/environments"
