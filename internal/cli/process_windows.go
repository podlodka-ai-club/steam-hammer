//go:build windows

package cli

import (
	"errors"
	"os/exec"
	"syscall"
)

const windowsProcessQueryLimitedInformation = 0x1000
const windowsStillActive = 259
const windowsErrorInvalidParameter syscall.Errno = 87

func configureDetachedProcess(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP}
}

func processAlive(pid int) (bool, error) {
	handle, err := syscall.OpenProcess(windowsProcessQueryLimitedInformation, false, uint32(pid))
	if err != nil {
		if errors.Is(err, windowsErrorInvalidParameter) {
			return false, nil
		}
		return false, err
	}
	defer syscall.CloseHandle(handle)

	var code uint32
	if err := syscall.GetExitCodeProcess(handle, &code); err != nil {
		return false, err
	}
	return code == windowsStillActive, nil
}
