class Reflector:

    def reflect(self, state):
        if not state.observations:
            return False
        observation = state.observations[-1]
        if observation:
            return True
        return False
