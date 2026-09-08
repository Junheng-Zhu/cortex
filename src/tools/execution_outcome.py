from typing import Any
class ExecutionOutcome():

    def __init__(self,success:bool,error:Exception,attempts:int,data:Any):
        self.success=success
        self.error_message=str(error)
        self.error_type=str(type(error).__name__)
        self.attempts=attempts
        self.data=data
