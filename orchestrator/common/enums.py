from enum import Enum


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class InstallationState(StringEnum):
    INSTALLED = "INSTALLED"
    NOT_INSTALLED = "NOT_INSTALLED"
    UNKNOWN = "UNKNOWN"


class RuntimeState(StringEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    STARTING = "STARTING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class StartupState(StringEnum):
    AUTOSTART_ENABLED = "AUTOSTART_ENABLED"
    AUTOSTART_DISABLED = "AUTOSTART_DISABLED"
    START_BLOCKED = "START_BLOCKED"
    UNKNOWN = "UNKNOWN"


class ServiceAction(StringEnum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"


class OperationStatus(StringEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.REJECTED,
            self.UNKNOWN,
        }


class JournalState(StringEnum):
    PREPARED = "PREPARED"
    DISPATCHING = "DISPATCHING"
    COMPLETED = "COMPLETED"


class AgentOnlineStatus(StringEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


class GroupState(StringEnum):
    DISARMED = "DISARMED"
    ARMED_IDLE = "ARMED_IDLE"
    WAITING_FOR_NODES = "WAITING_FOR_NODES"
    SETTLING = "SETTLING"
    BLOCKED_PRECONDITION = "BLOCKED_PRECONDITION"
    RUNNING = "RUNNING"


class RunTrigger(StringEnum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class RecoveryRunStatus(StringEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.UNKNOWN}


class RecoveryStepStatus(StringEnum):
    PENDING = "PENDING"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    STARTING = "STARTING"
    PROBING = "PROBING"
    READY = "READY"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in {self.READY, self.FAILED, self.BLOCKED, self.UNKNOWN}

