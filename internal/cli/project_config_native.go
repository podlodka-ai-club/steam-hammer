package cli

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

var nativePresetTierOrder = []string{"cheap", "default", "hard"}

func applyNativeProjectConfigDefaults(opts *commonOptions, fs flagState) error {
	projectConfig, err := loadNativeProjectConfig(*opts.dir, *opts.project)
	if err != nil {
		return err
	}
	if len(projectConfig) == 0 {
		return nil
	}

	projectDefaults := nativeProjectCLIDefaults(projectConfig)
	projectPreset := optionalConfigString(projectDefaults["preset"])
	applyNativeCommonDefaults(opts, fs, projectDefaults)
	if projectPreset != "" {
		presetDefaults, err := nativePresetCLIDefaults(projectConfig, projectPreset)
		if err != nil {
			return err
		}
		applyNativeCommonDefaults(opts, fs, presetDefaults)
	}
	if explicitPreset := strings.TrimSpace(*opts.preset); explicitPreset != "" && fs.wasPassed("preset") {
		presetDefaults, err := nativePresetCLIDefaults(projectConfig, explicitPreset)
		if err != nil {
			return err
		}
		applyNativeCommonDefaults(opts, fs, presetDefaults)
	}
	applyNativeGroomingDefaults(opts, fs, projectConfig)
	applyNativeBudgetCaps(opts, projectConfig)
	return nil
}

func loadNativeProjectConfig(cwd, explicitPath string) (map[string]any, error) {
	path := strings.TrimSpace(explicitPath)
	if path == "" {
		path = filepath.Join(defaultSourceDir(cwd), defaultProjectConfigName)
	} else if !filepath.IsAbs(path) {
		path = filepath.Join(defaultSourceDir(cwd), path)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("failed to read project config %s: %w", path, err)
	}
	var projectConfig map[string]any
	if err := json.Unmarshal(raw, &projectConfig); err != nil {
		return nil, fmt.Errorf("failed to parse project config %s: %w", path, err)
	}
	return projectConfig, nil
}

func nativeProjectCLIDefaults(projectConfig map[string]any) map[string]any {
	defaults, _ := projectConfig["defaults"].(map[string]any)
	cliDefaults := map[string]any{}
	for _, key := range []string{"tracker", "codehost", "runner", "agent", "model", "preset", "agent_timeout_seconds", "agent_idle_timeout_seconds", "max_attempts"} {
		if value, ok := defaults[key]; ok {
			cliDefaults[key] = value
		}
	}
	retry, _ := projectConfig["retry"].(map[string]any)
	if value, ok := retry["max_attempts"]; ok {
		cliDefaults["max_attempts"] = value
	}
	return cliDefaults
}

func nativePresetCLIDefaults(projectConfig map[string]any, presetName string) (map[string]any, error) {
	presetName = strings.TrimSpace(presetName)
	if presetName == "" {
		return nil, nil
	}
	presets, _ := projectConfig["presets"].(map[string]any)
	rawPreset, ok := presets[presetName]
	if !ok {
		return nil, fmt.Errorf("unknown preset %q in project config", presetName)
	}
	presetConfig, ok := rawPreset.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("project config key presets.%s must be an object", presetName)
	}
	cliDefaults := map[string]any{"preset": presetName}
	for _, key := range []string{"runner", "agent", "model", "agent_timeout_seconds", "agent_idle_timeout_seconds", "max_attempts"} {
		if value, ok := presetConfig[key]; ok {
			cliDefaults[key] = value
		}
	}
	return cliDefaults, nil
}

func applyNativeCommonDefaults(opts *commonOptions, fs flagState, defaults map[string]any) {
	setStringDefault(opts.tracker, fs, "tracker", defaults["tracker"])
	setStringDefault(opts.codehost, fs, "codehost", defaults["codehost"])
	setStringDefault(opts.runner, fs, "runner", defaults["runner"])
	setStringDefault(opts.agent, fs, "agent", defaults["agent"])
	setStringDefault(opts.model, fs, "model", defaults["model"])
	setStringDefault(opts.preset, fs, "preset", defaults["preset"])
	setIntDefault(opts.maxTry, fs, "max-attempts", defaults["max_attempts"])
	setIntDefault(opts.timeout, fs, "agent-timeout-seconds", defaults["agent_timeout_seconds"])
	setIntDefault(opts.idleTime, fs, "agent-idle-timeout-seconds", defaults["agent_idle_timeout_seconds"])
}

func applyNativeGroomingDefaults(opts *commonOptions, fs flagState, projectConfig map[string]any) {
	grooming, _ := projectConfig["grooming"].(map[string]any)
	if len(grooming) == 0 {
		return
	}
	setStringDefault(opts.groomingMode, fs, "grooming-mode", grooming["mode"])
	setBoolDefault(opts.groomingRequirePlanApproval, fs, "grooming-require-plan-approval", grooming["require_plan_approval"])
	setBoolDefault(opts.groomingAskQuestions, fs, "grooming-ask-questions", grooming["ask_questions"])
	setBoolDefault(opts.groomingAutoContinueAfterPlan, fs, "grooming-auto-continue-after-plan", grooming["auto_continue_after_plan"])
	setIntDefault(opts.groomingMaxRounds, fs, "grooming-max-rounds", firstPresent(grooming, "max_rounds", "max_questions"))
}

func applyNativeBudgetCaps(opts *commonOptions, projectConfig map[string]any) {
	budgets, _ := projectConfig["budgets"].(map[string]any)
	if len(budgets) == 0 {
		return
	}
	if maxTier := optionalConfigString(budgets["max_model_tier"]); maxTier != "" {
		if cappedPreset := capNativePresetToBudgetTier(projectConfig, *opts.preset, maxTier); cappedPreset != strings.TrimSpace(*opts.preset) {
			*opts.preset = cappedPreset
			if defaults, err := nativePresetCLIDefaults(projectConfig, cappedPreset); err == nil {
				applyNativeCommonDefaults(opts, nilFlagState{}, defaults)
			}
		}
	}
	if cap := positiveConfigInt(budgets["max_attempts_per_task"]); cap > 0 && *opts.maxTry > cap {
		*opts.maxTry = cap
	}
	if minutes := positiveConfigInt(budgets["max_runtime_minutes"]); minutes > 0 {
		capSeconds := minutes * 60
		if *opts.timeout == 0 || *opts.timeout > capSeconds {
			*opts.timeout = capSeconds
		}
	}
}

func capNativePresetToBudgetTier(projectConfig map[string]any, presetName, maxTier string) string {
	presetName = strings.TrimSpace(presetName)
	if presetName == "" || maxTier == "" {
		return presetName
	}
	presetRank := nativePresetTierRank(presetName)
	budgetRank := nativePresetTierRank(maxTier)
	if presetRank < 0 || budgetRank < 0 || presetRank <= budgetRank {
		return presetName
	}
	presets, _ := projectConfig["presets"].(map[string]any)
	for index := budgetRank; index >= 0; index-- {
		candidate := nativePresetTierOrder[index]
		if _, ok := presets[candidate]; ok {
			return candidate
		}
	}
	return presetName
}

func nativePresetTierRank(value string) int {
	value = strings.TrimSpace(value)
	for index, candidate := range nativePresetTierOrder {
		if value == candidate {
			return index
		}
	}
	return -1
}

func setStringDefault(target *string, fs flagState, name string, value any) {
	if target == nil || (fs != nil && fs.wasPassed(name)) {
		return
	}
	if normalized := optionalConfigString(value); normalized != "" {
		*target = normalized
	}
}

func setIntDefault(target *int, fs flagState, name string, value any) {
	if target == nil || (fs != nil && fs.wasPassed(name)) {
		return
	}
	if normalized := positiveConfigInt(value); normalized > 0 {
		*target = normalized
	}
}

func setBoolDefault(target *bool, fs flagState, name string, value any) {
	if target == nil || (fs != nil && fs.wasPassed(name)) {
		return
	}
	normalized, ok := optionalConfigBool(value)
	if !ok {
		return
	}
	*target = normalized
}

func optionalConfigBool(value any) (bool, bool) {
	typed, ok := value.(bool)
	if !ok {
		return false, false
	}
	return typed, true
}

func firstPresent(values map[string]any, keys ...string) any {
	for _, key := range keys {
		if value, ok := values[key]; ok {
			return value
		}
	}
	return nil
}

func optionalConfigString(value any) string {
	text, ok := value.(string)
	if !ok {
		return ""
	}
	return strings.TrimSpace(text)
}

func positiveConfigInt(value any) int {
	switch typed := value.(type) {
	case int:
		if typed > 0 {
			return typed
		}
	case float64:
		if typed >= 1 && typed == float64(int(typed)) {
			return int(typed)
		}
	}
	return 0
}
