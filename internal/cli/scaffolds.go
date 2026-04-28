package cli

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

const defaultProjectConfigName = "project-config.json"
const defaultLocalConfigName = "local-config.json"

const projectConfigScaffold = `{
  "defaults": {
    "preset": "default",
    "tracker": "github",
    "codehost": "github",
    "runner": "opencode",
    "agent": "build",
    "model": "openai/gpt-4o",
    "track_tokens": false,
    "token_budget": 20000,
    "agent_timeout_seconds": 1200,
    "agent_idle_timeout_seconds": 180,
    "max_attempts": 2
  },
  "workflow": {
    "commands": {
      "setup": "python -m pip install -r requirements.txt",
      "test": "python -m unittest",
      "lint": null,
      "build": null,
      "e2e": null
    },
    "hooks": {
      "pre_agent": null,
      "post_agent": null,
      "pre_pr_update": null,
      "post_pr_update": null
    },
    "verification": {
      "focused_commands": []
    },
    "readiness": {
      "required_checks": [],
      "required_approvals": 1,
      "require_review": true,
      "require_mergeable": true,
      "require_required_file_evidence": true
    },
    "merge": {
      "auto": false,
      "method": "squash"
    }
  },
  "retry": {
    "max_attempts": 2,
    "escalate_to_preset": "hard"
  },
  "scope": {
    "defaults": {
      "labels": {
        "allow": ["autonomous", "bug"],
        "deny": ["manual-only"]
      },
      "assignees": {
        "deny": ["human-only"]
      },
      "priority": {
        "allow": ["priority:high", "priority:medium"],
        "order": ["priority:high", "priority:medium", "priority:low"]
      },
      "freshness": {
        "max_age_days": 30,
        "max_idle_days": 14
      }
    }
  },
  "presets": {
    "cheap": {
      "runner": "opencode",
      "agent": "build",
      "model": "openai/gpt-4o-mini",
      "token_budget": 8000,
      "max_attempts": 1,
      "escalate_to_preset": "default"
    },
    "default": {
      "runner": "opencode",
      "agent": "build",
      "model": "openai/gpt-4o",
      "token_budget": 20000,
      "max_attempts": 2,
      "escalate_to_preset": "hard"
    },
    "hard": {
      "runner": "claude",
      "agent": "build",
      "model": "claude-sonnet-4-5",
      "token_budget": 40000,
      "max_attempts": 3,
      "escalate_to_preset": null
    }
  },
  "communication": {
    "verbosity": "normal"
  }
}
`

const localConfigScaffold = `{
  "preset": "default",
  "tracker": "github",
  "codehost": "github",
  "runner": "opencode",
  "agent": "build",
  "model": "openai/gpt-4o",
  "agent_timeout_seconds": 1200,
  "agent_idle_timeout_seconds": 180,
  "token_budget": 20000,
  "max_attempts": 2,
  "opencode_auto_approve": true,
  "fail_on_existing": false,
  "force_issue_flow": false,
  "skip_if_pr_exists": true,
  "skip_if_branch_exists": true,
  "force_reprocess": false,
  "sync_reused_branch": true,
  "sync_strategy": "rebase",
  "base_branch": "default",
  "create_child_issues": false
}
`

type scaffoldTarget struct {
	path     string
	contents string
}

func (a *App) runInit(args []string) int {
	fs := newFlagSet("init", a.err)
	dir := fs.String("dir", ".", "directory to write config scaffolds into")
	project := fs.String("project-config", defaultProjectConfigName, "path to the repository project config scaffold")
	local := fs.String("local-config", defaultLocalConfigName, "path to the user-local config scaffold")
	force := fs.Bool("force", false, "overwrite existing scaffold files")
	skipProject := fs.Bool("skip-project-config", false, "do not create the project config scaffold")
	skipLocal := fs.Bool("skip-local-config", false, "do not create the local config scaffold")

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected init argument: %s\n", fs.Arg(0))
		return 2
	}
	if *skipProject && *skipLocal {
		_, _ = fmt.Fprintln(a.err, "init has nothing to do: both scaffold outputs were skipped")
		return 2
	}

	targetDir := *dir
	if targetDir == "" {
		targetDir = "."
	}
	if err := os.MkdirAll(targetDir, 0o755); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create %s: %v\n", targetDir, err)
		return 1
	}

	var writes []scaffoldTarget
	if !*skipProject {
		writes = append(writes, scaffoldTarget{path: resolveScaffoldPath(targetDir, *project, defaultProjectConfigName), contents: projectConfigScaffold})
	}
	if !*skipLocal {
		writes = append(writes, scaffoldTarget{path: resolveScaffoldPath(targetDir, *local, defaultLocalConfigName), contents: localConfigScaffold})
	}

	for _, target := range writes {
		if err := writeScaffold(target.path, target.contents, *force); err != nil {
			_, _ = fmt.Fprintln(a.err, err.Error())
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "created %s\n", target.path)
	}

	return 0
}

func resolveScaffoldPath(dir, configuredPath, defaultName string) string {
	if configuredPath == "" {
		return filepath.Join(dir, defaultName)
	}
	if filepath.IsAbs(configuredPath) {
		return configuredPath
	}
	return filepath.Join(dir, configuredPath)
}

func writeScaffold(path, contents string, force bool) error {
	if !force {
		if _, err := os.Stat(path); err == nil {
			return fmt.Errorf("orchestrator: %s already exists (use --force to overwrite)", path)
		} else if !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("orchestrator: failed to inspect %s: %w", path, err)
		}
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("orchestrator: failed to create %s: %w", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(contents), 0o644); err != nil {
		return fmt.Errorf("orchestrator: failed to write %s: %w", path, err)
	}
	return nil
}
