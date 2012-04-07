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
import signal
import ctypes
import errno
import contextlib
import inspect
try:
    import _thread
except ImportError:
    import thread as _thread

libc = ctypes.PyDLL('libc.so.6') #need to use PyDLL since CDLL() releases the GIL
libc.__errno_location.restype = ctypes.POINTER(ctypes.c_int)

#typedef int (*Py_tracefunc)(PyObject *, struct _frame *, int, PyObject *);
Py_tracefunc = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.py_object, ctypes.py_object, ctypes.c_int, ctypes.py_object)

class PyThreadState(ctypes.Structure):
    _fields_ = [("next",                ctypes.POINTER(ctypes.c_int)),
                ("interp",              ctypes.POINTER(ctypes.c_int)),
                ('frame',               ctypes.py_object),
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

def only_thread():
    global debug__has_loaded_thread
    other_threads = [x[1] for x in sys._current_frames().items() if x[0] != _thread.get_ident() and x[0] != debug__has_loaded_thread]
    if len(other_threads) == 1:
        top = other_threads[0]
        back = top.f_back
        while back:
            top  = back
            back = top.f_back
        return (top.f_code.co_name == '_exitfunc')
    else:
        return (len(other_threads) == 0)

class ServerHandler(object):
    def __init__(self, parent_thread_tid):
        self.up      = 0
        self.line    = None
        self.thread  = parent_thread_tid

    def __readlines(self, filename):
        fd = libc.open(filename,os.O_RDONLY)
        file_data = ''
        try:
            rcode = 1024
            while rcode == 1024:
                data  = ctypes.create_string_buffer(1024)
                rcode = libc.read(fd, data, len(data))
                file_data += data.value
        finally:
            libc.close(fd)
        return file_data.split('\n') #TODO need to fix this to just extract the line(s) that are desired

    def __get_line(self, filename, lineno):
        return self.__readlines(filename)[lineno-1]

    def __format_stack(self, f, limit = None):
        ls = []
        n = 0
        while f is not None and (limit is None or n < limit):
            lineno = f.f_lineno
            co = f.f_code
            filename = co.co_filename
            name = co.co_name
            #linecache.checkcache(filename)
            line = self.__get_line(filename, lineno) #linecache.getline(filename, lineno, f.f_globals)
            if line: line = line.strip()
            else: line = None
            ls.append((filename, lineno, name, line))
            f = f.f_back
            n = n+1
        ls.reverse()
        return traceback.format_list(ls)

    def __current_frames(self):
        out = {}
        global debug__has_loaded_thread
        ignored_thread = debug__has_loaded_thread
        interp = ctypes.pythonapi.PyInterpreterState_Head()
        t      = ctypes.pythonapi.PyInterpreterState_ThreadHead(interp)
        while t != 0:
            t_p = ctypes.cast(t,ctypes.POINTER(PyThreadState))[0]
            if t_p.thread_id != _thread.get_ident() and t_p.thread_id != ignored_thread:
                out[t_p.thread_id] = t_p.frame
            t = ctypes.pythonapi.PyThreadState_Next(t)
        return out

    def __get_frame(self):
        frame = self.__current_frames()[self.thread]
        up = self.up-1
        while up >= 0 and frame is not None:
            frame = frame.f_back
            up -= 1
        return frame

    def do0_bt(self):
        """backtrace"""
        stacks = self.__format_stack(self.__current_frames()[self.thread])
        stacks = ['%s%s'%(['  ','**'][len(stacks)-1-self.up==i], x[2:]) for i,x in enumerate(stacks)]
        return '\n'.join(stacks)

    def do1_bt(self, thread='*'):
        frames = self.__current_frames().items()
        result = ''
        for frame_i in frames:
            tid, frame = frame_i
            stacks = self.__format_stack(frame)
            result += '-------- Thread %s\n%s\n'%(tid,'\n'.join(stacks))
        return result

    def do2_bt(self, thread):
        stacks = self.__format_stack(self.__current_frames()[int(thread)])
        stacks = [''.join(x) for i,x in enumerate(stacks)]
        return '\n'.join(stacks)

    def do_threads(self):
        return '\n'.join( [str(x) for x in self.__current_frames().values()] )

    def do0_thr(self):
        """Set / Get Thread"""
        return self.thread

    def do1_thr(self, thread):
        frames = [str(x) for x in self.__current_frames().values()]
        if thread not in frames:
            return 'Error: No such thread'
        else:
            self.up     = 0
            self.line   = None
            self.thread = int(thread)
            return 'Switched to thread %s'%thread

    def do_up(self):
        self.up += 1
        frame = self.__get_frame()
        if frame is None:
            self.up -= 1
            return 'Error: Nothing higher'
        else:
            self.line = None
            return '\n'.join(self.__format_stack(frame, 1))

    def do_down(self):
        if self.up > 0:
            self.up -= 1
            frame = self.__get_frame()
            self.line = None
            return '\n'.join(self.__format_stack(frame, 1))
        else:
            return 'Error: Nothing lower'

    def __get_variable(self, variable):
        frame = self.__get_frame()
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

    def do_raise(self, exctype):
        exctype_cls = self.__get_variable(exctype)
        if not inspect.isclass(exctype_cls):
            raise TypeError("Only types can be raised (not instances)")
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(self.thread), ctypes.py_object(exctype_cls))
        if res == 0:
            raise ValueError("invalid thread id")
        elif res != 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(self.thread), None)
            raise SystemError("PyThreadState_SetAsyncExc failed")
        return 'Asynchronously raising an exception of type %s'%exctype

    def do_p(self, variable):
        return repr(self.__get_variable(variable))

    def do_dir(self, variable):
        return repr(dir(self.__get_variable(variable)))

    def do_locals(self):
        frame = self.__get_frame()
        return '\n'.join(frame.f_locals.keys())

    def do_globals(self):
        frame = self.__get_frame()
        return '\n'.join(frame.f_globals.keys())

    def __list(self):
        frame = self.__get_frame()
        lines = self.__readlines(frame.f_code.co_filename)
        if self.line < 5:
            min_line_no = 0
            max_line_no = min(len(lines)-1,10)
        else:
            min_line_no = self.line-5
            max_line_no = min(len(lines)-1,self.line+5)
        return ''.join(['%5i %s\n'%(i+1,lines[i]) for i in xrange(min_line_no,max_line_no)])[:-1]
    def do0_list(self, line='-'):
        self.line = max(self.line-10,0)
        return self.__list()
    def do1_list(self):
        frame = self.__get_frame()
        self.line = frame.f_lineno
        return self.__list()
    def do2_list(self, line):
        self.line = int(line)
        return self.__line()

    def mux(self, command, *args_in):
        if command == 'exit':
            return 'exit'
        elif hasattr(self, 'do_%s'%command):
            return getattr(self, 'do_%s'%command)(*args_in)
        else:
            items = sorted(dir(self))
            for item in items:
                if item.startswith('do') and item[-len(command)-1:] == '_%s'%command:
                    fn = getattr(self, item)
                    args, varargs, keywords, defaults = inspect.getargspec(fn)
                    if len(args)-1 == len(args_in): # -1 for self
                        if defaults is None:
                            return fn(*args_in)
                        else:
                            good = True
                            for i,default in enumerate(reversed(defaults)):
                                if args_in[-i-1] != default:
                                    good = False
                            if good:
                                return fn(*args_in)

def tracefunc(state, frame, what, arg):
    # details = (frame.f_code.co_filename,frame.f_lineno, frame.f_code.co_name)
    sys.stdout.flush()
    return 0

def setup_thread_tracer(state, setting):
    interp = ctypes.pythonapi.PyInterpreterState_Head()
    t      = ctypes.pythonapi.PyInterpreterState_ThreadHead(interp)
    while t != 0:
        t_p = ctypes.cast(t,ctypes.POINTER(PyThreadState))[0]
        if t_p.thread_id != _thread.get_ident() and t_p.thread_id != debug__has_loaded_thread:
            if setting:
                t_p.c_tracefunc = Py_tracefunc(tracefunc)
                t_p.c_traceobj  = state
                t_p.use_tracing = 1
                t_p.tracing     = 0
            else:
                t_p.use_tracing = 0
        t = ctypes.pythonapi.PyThreadState_Next(t)

global __pydebug_breakpoint_hit

def continue_threads(state):
    global __pydebug_breakpoint_hit
    _Py_Ticker = ctypes.cast(ctypes.pythonapi._Py_Ticker,ctypes.POINTER(ctypes.c_int))
    __pydebug_breakpoint_hit = False
    try:
        setup_thread_tracer(state, True)
        while not __pydebug_breakpoint_hit and not only_thread():
            _Py_Ticker[0] = 0    #switch to another thread
            _Py_Ticker[0] = 100
        _Py_Ticker[0] = 2**31-1
    finally:
        setup_thread_tracer(state, False) # if connection breaks, I should call this too

class pollfd(ctypes.Structure):
    _fields_ = [("fd",          ctypes.c_int),
                ("events",      ctypes.c_short),
                ('revents',     ctypes.c_short)
                ]

def handle_particular_user(conn,parent_thread,parent_thread_tid):
    sh = ServerHandler(parent_thread_tid)
    poll = pollfd()
    poll.fd      = conn.fileno()
    poll.events  = select.POLLIN
    poll.revents = select.POLLIN
    request_pending = ''
    restored_sigint = ctypes.pythonapi.PyOS_setsig(signal.SIGINT, 1) # 1 == SIG_IGN
    _Py_Ticker = ctypes.cast(ctypes.pythonapi._Py_Ticker,ctypes.POINTER(ctypes.c_int))
    try:
        while not only_thread():
            _Py_Ticker[0] = 2**31-1 # reset to max ticks remaining
            ret = libc.poll(ctypes.byref(poll), 1, 300)  # pointer to pollfd object, 1 pollfd object, 300 ms timeout
            if ret > 0:
                data = ctypes.create_string_buffer(1024)
                count = libc.recv(conn.fileno(), data, len(data), 0)
                if count < 0:
                    err_name = errno.errorcode[ libc.__errno_location()[0] ]
                    libc.printf('ERROR -- %s\n', err_name)
                    return
                if count == 0:
                    return
                request_pending += data.value
            elif ret < 0:
                err_name = errno.errorcode[ libc.__errno_location()[0] ]
                libc.printf('ERROR -- %s\n', err_name)
                return
            if request_pending.find('\n') >= 0:
                request_is = request_pending.split('\n')
                request_pending = '\n'.join(request_is[1:])
                request = pickle.loads(base64.b64decode(request_is[0]))
                try:
                    reload(sys)
                    output = sh.mux(request['command'], *request['args'])
                except:
                    output = traceback.format_exc()
                out_enc = '%s\n'%base64.b64encode(pickle.dumps(output))
                libc.send(conn.fileno(), ctypes.c_char_p(out_enc), len(out_enc))
                if output == 'exit':
                    return
    finally:
        ctypes.pythonapi.PyOS_setsig(signal.SIGINT, restored_sigint)
        _Py_Ticker[0] = 0 #yield to other threads

# the only purpose this thread serves is to make sure that (static global int) _Py_TracingPossible in ceval.c is set to >= 1
def ignored_tracing_function(frame, event, arg):
    return ignored_tracing_function
def ignored_tracing_thread(lock):
    global debug__has_loaded_thread
    debug__has_loaded_thread = _thread.get_ident()
    sys.settrace(ignored_tracing_function)
    lock.acquire()

def cmd_server(parent_thread, parent_thread_tid):
    mypid = os.getpid()
    path = '/tmp/py.debug.%s'%mypid
    try:
        lock = threading.Lock()
        igtt = threading.Thread(target=ignored_tracing_thread, args=(lock,))
        igtt.start()
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
                except:
                    libc.printf(traceback.format_exc())
    finally:
        lock.release()
        os.unlink(path)

class DebugShell(cmd.Cmd):
    """Command line for debugging attached pid process"""
    prompt = '(py.debug) '

    def __init__(self, pid):
        self.pid  = pid
        self.conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.conn.connect('/tmp/py.debug.%s'%self.pid)
        self.poll = select.poll()
        self.poll.register(self.conn, select.POLLIN|select.POLLHUP)
        cmd.Cmd.__init__(self)

    def client_handle_request(self, command, *args):
        request = {'command': command, 'args': args}
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

    def default(self, line):
        items = line.strip().split(' ')
        libc.printf(self.client_handle_request(*items))
        libc.printf('\n')
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
    global debug__has_loaded_thread
    try:
        x = debug__has_loaded_thread
    except NameError:
        debug__has_loaded_thread = True
        t = threading.Thread(target=cmd_server, args=(threading.currentThread(),_thread.get_ident()))
        t.start()
