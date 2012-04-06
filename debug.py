from __future__ import with_statement
import os
import sys
import threading
import pickle
import traceback
import shutil
import time
import cmd
import uuid
import socket
import base64
import select
import ctypes 
import contextlib
import inspect
try:
    import _thread
except ImportError:
    import thread as _thread

@contextlib.contextmanager
def suspend_other_threads():
    orig_checkinterval = sys.getcheckinterval()
    try:
#        sys.setcheckinterval(orig_checkinterval+100) # in case we lose control right after setting this
#        sys.setcheckinterval(2**31-1) #TODO this should be blocking, but wont be in this case
        yield None
    finally:
#        sys.setcheckinterval(orig_checkinterval)
        pass

runnable_commands = {}
def command(fn):
    runnable_commands[fn.__name__] = fn

def get_frame(state):
    frame = sys._current_frames()[state['thread']]
    up = state['up']-1
    while up >= 0 and frame is not None:
        frame = frame.f_back
        up -= 1
    return frame

@command
def bt(state):
    reload(sys)
    stacks = traceback.format_stack(sys._current_frames()[state['thread']])
    stacks = ['%s%s'%(['  ','**'][len(stacks)-1-state['up']==i], x[2:]) for i,x in enumerate(stacks)]
    return '\n'.join(stacks)

@command
def allbt(state):
    reload(sys)
    frames = [x for x in sys._current_frames().items() if x[0] != _thread.get_ident()]
    result = ''
    for frame_i in frames:
        tid, frame = frame_i
        stacks = traceback.format_stack(frame)
        result += '-------- Thread %s\n%s\n'%(tid,'\n'.join(stacks))
    return result

@command
def thbt(state, thread):
    reload(sys)
    stacks = traceback.format_stack(sys._current_frames()[thread])
    stacks = [repr(x) for i,x in enumerate(stacks)]
    return '\n'.join(stacks)

@command
def threads(state):
    frames = [str(x[0]) for x in sys._current_frames().items() if x[0] != _thread.get_ident()]
    return '\n'.join(frames)

@command
def get_thread(state):
    return state['thread']

@command
def set_thread(state, thread):
    frames = [str(x[0]) for x in sys._current_frames().items() if x[0] != _thread.get_ident()]
    if thread not in frames:
        return 'Error: No such thread'
    else:
        state['up'] = 0
        state['line'] = None
        state['thread'] = int(thread)
        return 'Switched to thread %s'%thread

@command
def up(state):
    state['up'] += 1
    frame = get_frame(state)
    if frame is None:
        state['up'] -= 1
        return 'Error: Nothing higher'
    else:
        state['line'] = None
        return '\n'.join(traceback.format_stack(frame, 1))

@command
def down(state):
    if state['up'] > 0:
        state['up'] -= 1
        frame = get_frame(state)
        state['line'] = None
        return '\n'.join(traceback.format_stack(frame, 1))
    else:
        return 'Error: Nothing lower'
 
def _async_raise(tid, exctype):
    """raises the exception, performs cleanup if needed"""
    if not inspect.isclass(exctype):
        raise TypeError("Only types can be raised (not instances)")
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(tid), ctypes.py_object(exctype))
    if res == 0:
        raise ValueError("invalid thread id")
    elif res != 1:
        # """if it returns a number greater than one, you're in trouble, 
        # and you should call it again with exc=NULL to revert the effect"""
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(tid), None)
        raise SystemError("PyThreadState_SetAsyncExc failed")

def get_variable(state, variable):
    frame = get_frame(state)
    var_names = variable.split('.')
    if var_names[0] in frame.f_locals:
        res = frame.f_locals[var_names[0]]
    elif var_names[0] in frame.f_globals:
        res = frame.f_globals[var_names[0]]
    elif var_names[0] in dir(frame.f_globals['__builtins__']):
        res = getattr(frame.f_globals['__builtins__'],var_names[0])
    else:
        raise RuntimeError('Error: couldnt find variable "%s" in scope'%variable)
    for name in var_names[1:]:
        res = getattr(res, name)
    return res

@command
def throw(state, variable):
    _async_raise(state['thread'], get_variable(state, variable))
    return 'Asynchronously raising an exception of type %s'%variable

@command
def printv(state, variable):
    return repr(get_variable(state,variable))

@command
def get_dir(state, variable):
    return repr(dir(get_variable(state,variable)))

@command
def get_locals(state):
    frame = get_frame(state)
    return '\n'.join(frame.f_locals.keys())

@command
def get_globals(state):
    frame = get_frame(state)
    return '\n'.join(frame.f_globals.keys())

@command
def get_list(state, line):
    frame = get_frame(state)
    lines = open(frame.f_code.co_filename).readlines()
    if line == '-' and state['line'] is not None:
        state['line'] = max(state['line']-10, 0)
    elif line != '':
        state['line'] = int(line)
    elif state['line'] is None:
    	state['line'] = frame.f_lineno
    else:
        state['line'] = min(state['line']+10, len(lines)-6)

    if state['line'] < 5:
        min_line_no = 0
        max_line_no = min(len(lines)-1,10)
    else:
        min_line_no = state['line']-5
        max_line_no = min(len(lines)-1,state['line']+5)
    return ''.join(['%5i %s'%(i+1,lines[i]) for i in xrange(min_line_no,max_line_no)])

def only_thread():
    other_threads = [x[1] for x in sys._current_frames().items() if x[0] != _thread.get_ident()]
    out = len(other_threads) == 0
    if len(other_threads) == 1:
        top = other_threads[0]
        back = top.f_back
        while back:
            top  = back
            back = top.f_back
        if top.f_code.co_name == '_exitfunc':
            return True
    return out

def handle_particular_user(conn,parent_thread,parent_thread_tid):
    state = {'thread': parent_thread_tid, 'up': 0, 'line': None}
    poll = select.poll()
    poll.register(conn, select.POLLIN)
    request_pending = ''
    while not only_thread():# parent_thread.isAlive():
        ret = poll.poll(.3)
        for fd, event in ret:
            data = conn.recv(1024)
            if not data:
                return
            request_pending += data
        if request_pending.find('\n') >= 0:
            request_is = request_pending.split('\n')
            request_pending = '\n'.join(request_is[1:])
            request = pickle.loads(base64.b64decode(request_is[0]))
            output = None
            if 'command' in request and 'args' in request and 'kwds' in request:
                cmd  = request['command']
                args = request['args']
                kwds = request['kwds']
                if cmd == 'exit':
                    out_enc = '%s\n'%base64.b64encode(pickle.dumps(True))
                    conn.send(out_enc)
                    return
                if cmd in runnable_commands:
                    try:
                        output = runnable_commands[cmd](state, *args,**kwds)
                    except:
                        output = traceback.format_exc()
            out_enc = '%s\n'%base64.b64encode(pickle.dumps(output))
            conn.send(out_enc)

def cmd_server(parent_thread, parent_thread_tid):
    mypid = os.getpid()
    path = '/tmp/py.debug.%s'%mypid
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        os.chmod(path, 0777)
        s.listen(1)
        poll = select.poll()
        poll.register(s, select.POLLIN)
        while not only_thread(): #parent_thread.isAlive():
            ret = poll.poll(.3)
            if len(ret) > 0:
                conn, addr = s.accept()
                with suspend_other_threads():
                    try:
                        handle_particular_user(conn,parent_thread,parent_thread_tid)
                    except socket.error:
                        pass
    finally:
        os.unlink(path)

class DebugShell(cmd.Cmd):
    """Command line for debugging attached pid process"""

    prompt = '(py.debug) '

    def __init__(self, pid):
        self.pid  = pid
        self.conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.conn.connect('/tmp/py.debug.%s'%self.pid)
        self.poll = select.poll()
        self.poll.register(self.conn, select.POLLIN)
        cmd.Cmd.__init__(self)

    def client_handle_request(self, command, *args, **kwds):
        request = {'command': command, 'args': args, 'kwds': kwds}
        req_enc = '%s\n'%base64.b64encode(pickle.dumps(request))
        self.conn.send(req_enc)
        request_pending = ''
        while request_pending.find('\n') < 0:
            ret = self.poll.poll(.3)
            for fd, event in ret:
                data = self.conn.recv(1024)
                if not data:
                    break
                request_pending += data
        return pickle.loads(base64.b64decode(request_pending.split('\n')[0]))

    def do_bt(self,line):
        if line.strip() == '':
            print self.client_handle_request('bt')
        elif line.strip() == '*':
            print self.client_handle_request('allbt')
        else:
            print self.client_handle_request('thbt',int(line))

    def do_threads(self, line):
        print self.client_handle_request('threads')

    def do_thr(self, line):
        if line.strip() == '':
            print self.client_handle_request('get_thread')
        else:
            print self.client_handle_request('set_thread',line.strip())

    def do_up(self, line):
        print self.client_handle_request('up')

    def do_down(self, line):
        print self.client_handle_request('down')

    def do_raise(self, line):
        print self.client_handle_request('throw', line)

    def do_p(self, name):
        print self.client_handle_request('printv',name)

    def do_print(self, name):
        print self.client_handle_request('printv',name)

    def do_dir(self, name):
        print self.client_handle_request('get_dir',name)

    def do_locals(self, line):
        print self.client_handle_request('get_locals')

    def do_globals(self, line):
        print self.client_handle_request('get_globals')

    def do_list(self, line):
        print self.client_handle_request('get_list',line),

    def do_EOF(self, line):
        self.client_handle_request('exit')
        return True

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print >>sys.stderr, 'Usage: %s pid'%sys.argv[0]
    else:
        DebugShell(sys.argv[1]).cmdloop()
else:
    print 'main thread ident=',_thread.get_ident()
    global debug__has_loaded_thread
    try:
        x = debug__has_loaded_thread
    except NameError:
        debug__has_loaded_thread = True
        t = threading.Thread(target=cmd_server, args=(threading.currentThread(),_thread.get_ident()))
        t.start()

