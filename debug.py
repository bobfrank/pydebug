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
    if len(other_threads) == 1:
        top = other_threads[0]
        back = top.f_back
        while back:
            top  = back
            back = top.f_back
        return (top.f_code.co_name == '_exitfunc')
    else:
        return (len(other_threads) == 0)

def set_max_ticks_remaining():
    # sys.setcheckinterval() doesn't cut it -- read ceval.c to see details
    # I only noticed this being reset in Py_AddPendingCall and after it decrements down to 0
    _Py_Ticker = ctypes.cast(ctypes.pythonapi._Py_Ticker,ctypes.POINTER(ctypes.c_int))[0]
    _Py_Ticker = 2**31-1

def run_next_command(state):
    # note that we run to the next command only for the selected thread, others have to run just some number of ticks
    state['up'] = 0
    frame = get_frame(state)
    saved_breaks = state['breaks']
    try:
        state['breaks'] = [(frame.f_code.co_filename,frame.f_lineno)]
        continue_threads(state)
    finally:
        state['breaks'] = saved_breaks

def tracefunc(state, frame, what, arg):
    print >>sys.stdout, 'state=',state,'frame=',frame,'what=',what,'arg=',arg
    print (frame.f_code.co_filename,frame.f_lineno, frame.f_code.co_name)
    sys.stdout.flush()
    return 0

def setup_thread_tracer(state, setting):
    interp = ctypes.pythonapi.PyInterpreterState_Head()
    t      = ctypes.pythonapi.PyInterpreterState_ThreadHead(interp)
    while t != 0:
        t_p = ctypes.cast(t,ctypes.POINTER(PyThreadState))[0]
        if t_p.thread_id != _thread.get_ident():
            if setting:
                print 'setting tracefunc for',t_p
                print t_p.use_tracing
                print t_p.tracing
                t_p.c_tracefunc = Py_tracefunc(tracefunc)
                t_p.c_traceobj  = state
                t_p.use_tracing = 1
                t_p.tracing     = 0
            else:
                print 'clearing tracefunc for',t_p
                t_p.use_tracing = 0
        t = ctypes.pythonapi.PyThreadState_Next(t)

global __pydebug_breakpoint_hit

@command
def continue_threads(state):
    global __pydebug_breakpoint_hit
    _Py_Ticker = ctypes.cast(ctypes.pythonapi._Py_Ticker,ctypes.POINTER(ctypes.c_int))[0]
    __pydebug_breakpoint_hit = False
    try:
        setup_thread_tracer(state, True)
        while not __pydebug_breakpoint_hit and not only_thread():
            _Py_Ticker = 0    #switch to another thread
            _Py_Ticker = 1000
        _Py_Ticker = 2**31-1
    finally:
        setup_thread_tracer(state, False)

#typedef int (*Py_tracefunc)(PyObject *, struct _frame *, int, PyObject *);
Py_tracefunc = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.py_object, ctypes.py_object, ctypes.c_int, ctypes.py_object)

class PyThreadState(ctypes.Structure):
    _fields_ = [("next",                ctypes.POINTER(ctypes.c_int)),
                ("interp",              ctypes.POINTER(ctypes.c_int)),
                ('frame',               ctypes.POINTER(ctypes.c_int)),
                ('recursion_depth',     ctypes.c_int),
                ('tracing',             ctypes.c_int),
                ('use_tracing',         ctypes.c_int),
                ('c_profilefunc',       Py_tracefunc),
                ('c_tracefunc',         Py_tracefunc),
                ('c_profileobj',        ctypes.py_object),
                ('c_traceobj',          ctypes.py_object),
                ('curexc_type',         ctypes.py_object),
                ('curexc_value',        ctypes.py_object),
                ('curexc_traceback',    ctypes.py_object),
                ('exc_type',            ctypes.py_object),
                ('exc_value',           ctypes.py_object),
                ('exc_traceback',       ctypes.py_object),
                ('dict',                ctypes.py_object),
                ('tick_counter',        ctypes.c_int),
                ('gilstate_counter',    ctypes.c_int),
                ('async_exc',           ctypes.py_object),
                ('thread_id',           ctypes.c_long)
                ]

def handle_particular_user(conn,parent_thread,parent_thread_tid):
    state = {'thread': parent_thread_tid, 'up': 0, 'line': None, 'breaks': []}
    poll = select.poll()
    poll.register(conn, select.POLLIN)
    request_pending = ''
    while not only_thread():
        set_max_ticks_remaining()
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

    def do_cont(self, line):
        self.client_handle_request('continue_threads')

    def do_rit(self, line):
        self.client_handle_request('rit')

    def do_EOF(self, line):
        self.client_handle_request('exit')
        return True

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print >>sys.stderr, 'Usage: %s pid'%sys.argv[0]
    else:
        DebugShell(sys.argv[1]).cmdloop()
else:
    global __pydebug_breakpoint_hit
    __pydebug_breakpoint_hit = False
    print 'main thread ident=',_thread.get_ident()
    global debug__has_loaded_thread
    try:
        x = debug__has_loaded_thread
    except NameError:
        debug__has_loaded_thread = True
        t = threading.Thread(target=cmd_server, args=(threading.currentThread(),_thread.get_ident()))
        t.start()

