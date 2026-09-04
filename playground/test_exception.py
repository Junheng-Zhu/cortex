def test():
    # raise ValueError

    try: 1/0
    except Exception :
        print("出错了")


test()