import debug
import threading
import os
import time
def x(num):
    print 'num=',num
    for i in xrange(1000):
        time.sleep(.1)
if __name__ == '__main__':
    p = os.getpid()
    t = threading.Thread(target=x,args=(0,))
    t.start()
    x(p)

