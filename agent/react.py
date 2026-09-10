class ReActEngine:


    def think(self,state):


        thought = (
            "分析用户需求，"
            "判断是否需要工具"
        )


        state.thoughts.append(
            thought
        )


        return thought