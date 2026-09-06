class FlakyTool:
    def __init__(self):
        self.attempts = 0

    def execute(self)->bool:
        self.attempts += 1
        if self.attempts == 1:
            raise Exception("第一次异常")
        elif self.attempts==2:
            print("success")
            return True

# max_retries=1
max_retries=2
tool=FlakyTool()

for i in range(max_retries+1):

    try: 
        print("attempts="+str(tool.attempts))
        tool.execute()
    except Exception as e:
        print(str(e))
        
    else:
        break

print("结束时attempts="+str(tool.attempts))
""" attempts=0
第一次异常
attempts=1
success
结束时attempts=2 """

class CounterTool():
    def __init__(self):
        self.count=0
        

    def _should_retry(self)->bool:
        return True

    def _execute_with_retry(self,max_retries:int,function:function,arguments):
        for i in range(max_retries+1):
            self.count +=1
            try:function(arguments)
            except Exception:
                if not self._should_retry:
                    break
                else:
                    raise
            else:
                break





    
        
