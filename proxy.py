#!/bin/python3

# debugging
import traceback

import os
import argparse

# For networking
import socket
import select

# Thread safe data structure to hold messages we want to send
from queue import SimpleQueue

# For creating multiple threads
from threading import Thread
from time import sleep

# This allows auto completion and history browsing
try:
    import gnureadline as readline
except ImportError:
    import readline

# This allows us to reload a python file
from importlib import reload

# This is where users may do live edits and alter the behavior of the proxy.
import proxyparser as parser

class Proxy(Thread):
    def __init__(self, application, bindAddr: str, remoteAddr: str, localPort: int, remotePort: int):
        super().__init__()

        self.application = application

        self.running = False
        self.identifier = f"{bindAddr}:{localPort} -> {remoteAddr}:{remotePort}"

        self.bindAddr = bindAddr
        self.remoteAddr = remoteAddr
        self.localPort = localPort
        self.remotePort = remotePort
        
        # Sockets
        self.bindSocket = None
        self.server = None
        self.client = None

        self.bind(self.bindAddr, self.localPort)
        return

    def run(self) -> None:
        # after client disconnected await a new client connection
        while True:

            print(f"[proxy({self.identifier})] setting up.")
            # Wait for a client.
            self.waitForClient()
            
            # Client has connected.
            ch, cp = self.getClient()
            self.identifier = f"{ch}:{cp} -> {self.remoteAddr}:{self.remotePort}"
            print(f"[proxy({self.identifier})] client connected. Connecting to remote host.")
            
            # Connect to the remote host after a client has connected.
            self.connect()
            print(f"[proxy({self.identifier})] connection established.")

            # Start client and server socket handler threads.
            if self.client is not None:
                self.client.start()
            if self.server is not None:
                self.server.start()
            
            self.running = True
        return

    def sendData(self, destination: str, data: bytes) -> None:
        sh = self.client if destination == 'c' else self.server
        if sh is None:
            return
        sh.send(data)
        return
    
    def sendToServer(self, data: bytes) -> None:
        self.sendData('s', data)
        return
    
    def sendToClient(self, data: bytes) -> None:
        self.sendData('c', data)
        return

    def getClient(self) -> (str, int):
        ret = (None, None)
        if self.client is not None:
            ret = (self.client.host, self.client.port)
        return ret

    def getServer(self) -> (str, int):
        ret = (None, None)
        if self.server is not None:
            ret = (self.server.host, self.server.port)
        return ret
    
    def bind(self, host: str, port: int) -> None:
        print(f"[proxy({self.identifier})] Starting listening socket on {host}:{port}")
        self.bindSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.bindSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.bindSocket.bind((host, port))
        self.bindSocket.listen(1)

    def waitForClient(self) -> None:
        oldClient = self.client
        sock, addr = self.bindSocket.accept()
        self.client = SocketHandler(sock, (self.remoteAddr, self.remotePort), 'c', self)
        
        # Disconnect the old client if there was one.
        if oldClient is not None:
            oldClient.stop()
            oldClient.join()
            oldClient = None
            # Also disconnect from the server for a brand new connection.
            if self.server is not None:
                self.server.stop()
                self.server.join()
                self.server = None

        return
    
    def connect(self) -> None:
        print(f"[proxy({self.identifier})] Connecting to {self.remoteAddr}:{self.remotePort}")
        if self.server is not None:
            print(f"[proxy({self.identifier})] Already connected to remote host.")
            return

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self.remoteAddr, self.remotePort))
            self.server = SocketHandler(sock, self.getClient(), 's', self)
        except Exception as e:
            print('[proxy({})] Unable to connect to server {}:{}. {}'.format(self.identifier, self.remoteAddr, self.remotePort, e))
            if self.client is not None:
                self.client.stop()
                self.client = None

        return

    def disconnect(self) -> None:
        if self.client is not None:
            self.client.stop()
            self.client = None
        
        if self.server is not None:
            self.server.stop()
            self.server = None
        
        return

###############################################################################

# This class owns a socket, receives all it's data and accepts data into a queue to be sent to that socket.
class SocketHandler(Thread):
    def __init__(self, sock: socket.socket, other: (str, int), role: str, proxy: Proxy):
        super().__init__()
        
        self.sock = sock   # The socket
        self.other = other # The other socket host and port for output in the parser
        self.role = role   # Either 'c' or 's'
        self.proxy = proxy # To pass to the parser
        
        # Get this once, so there is no need to check for validity of the socket later.
        self.host, self.port = sock.getpeername()
        
        # Simple, thread-safe data structure for our messages to the socket to be queued into.
        self.dataQueue = SimpleQueue()
        
        self.running = False

        # Set socket non-blocking. recv() will return if there is no data available.
        sock.setblocking(True)
    
    def send(self, data: bytes) -> None:
        self.dataQueue.put(data)
        return

    def getHost(self) -> str:
        return self.host

    def getPort(self) -> int:
        return self.port

    def stop(self) -> None:
        # Cleanup of the socket is in the thread itself, in the run() function, to avoid the need for locks.
        self.running = False
        return

    def checkAlive(self) -> (bool, bool, bool):
        try:
            readyToRead, readyToWrite, inError = select.select([self.sock,], [self.sock,], [], 3)
        except select.error as e:
            self.stop()
        return (len(readyToRead) > 0, len(readyToWrite) > 0, inError)

    def sendQueue(self) -> bool:
        abort = False
        try:
            # Send any data which may be in the queue
            while not self.dataQueue.empty():
                message = self.dataQueue.get()
                #print(f">>> Sending {len(message)} Bytes to {self.role}")
                self.sock.sendall(message)
        except Exception as e:
            print('[EXCEPT] - xmit data to {} [{}:{}]: {}'.format(self.role, self.host, self.port, e))
            abort = True
        return abort

    def run(self) -> None:
        self.running = True
        while self.running:
            # Receive data from the host.
            data = False
            abort = False

            readyToRead, readyToWrite, inError = self.checkAlive()

            if readyToRead:
                try:
                    data = self.sock.recv(4096)
                    if len(data) == 0:
                        raise IOError("Socket Disconnected")
                except BlockingIOError as e:
                    # No data was available at the time.
                    pass
                except Exception as e:
                    print('[EXCEPT] - recv data from {} [{}:{}]: {}'.format(self.role, self.host, self.port, e))
                    abort = True
            
            # If we got data, parse it.
            if data:
                try:
                    # Parse the data. The parser may enqueue any data it wants to send on to the server.
                    # The parser adds any packages it actually wants to forward for the server to the queue.
                    parser.parse(data, (self.host, self.port), self.other, self.role, self.proxy)
                except Exception as e:
                    print('[EXCEPT] - parse data from {} [{}:{}]: {}'.format(self.role, self.host, self.port, e))
                    self.stop()
            
            # Send the queue
            queueEmpty = self.dataQueue.empty()
            readyToRead, readyToWrite, inError = self.checkAlive()
            abort2 = False
            if not queueEmpty and readyToWrite:
                abort2 = self.sendQueue()
            
            if abort or abort2:
                self.proxy.disconnect()

            # Prevent the CPU from Melting
            # Sleep if we didn't get any data or if we didn't send
            if not data and (queueEmpty or not readyToWrite):
                sleep(0.001)
        
        # Stopped, clean up socket.
        if self.sock is None:
            return
        # Send all remaining messages.
        sleep(0.1)
        self.sendQueue()
        sleep(0.1)

        self.sock.close()
        self.sock = None
        return

###############################################################################

class Completer():
    def __init__(self):
        self.origline = ""
        self.begin = 0
        self.end = 0
        self.being_completed = ""
        self.words = []
        
        self.candidates = []

    def complete(self, text: str, state: int) -> str:
        try:
            response = None
            # First tab press for this string (state is 0), build the list of candidates.
            if state == 0:
                # Get line buffer info
                self.origline = readline.get_line_buffer()
                self.begin = readline.get_begidx()
                self.end = readline.get_endidx()
                self.being_completed = self.origline[self.begin:self.end]
                self.words = self.origline.split(' ')

                self.candidates = []
                
                # Check these first, check history only if no matches found.
                if self.getWordIdx() == 0:
                    # Root commands only make sense at the start of the line.
                    self.getRootCandidates()
                else:
                    self.getFileCandidates()
                    # Show history completions only if there are not too many paths to complete or if there is no entry at all.
                    if len(self.candidates) <= 8 or len(self.being_completed) == 0:
                        self.getHistoryCandidates()
            # Return the answer!
            try:
                response = self.candidates[state]
            except IndexError as e:
                response = None
        except Exception as e:
            print(e)
            print(traceback.format_exc())
        return response

    def getRootCandidates(self) -> None:
        self.candidates.extend( [
                s
                for s in parser.buildCommandDict().keys()
                if s and s.startswith(self.being_completed)
            ]
        )
        return

    def getHistoryCandidates(self) -> None:
        # Get candidates from the history
        history = [readline.get_history_item(i) for i in range(0, readline.get_current_history_length())]
        for historyline in history:
            if historyline is None:
                continue

            self.candidates.extend([
                    s
                    for s in historyline.split(" ")
                    if s and s.startswith(self.being_completed)
                ])
        return

    def getFileCandidates(self) -> None:
        # Append candidates for files
        # Find which word we are current completing
        word = self.words[self.getWordIdx()]

        # Find which directory we are in
        directory = "./"
        filenameStart = ""
        if word:
            # There is at least some text being completed.
            if word.find("/") >= 0:
                # There is a path delimiter in the string, we need to assign the directory and the filename start both.
                directory = word[:word.rfind("/")] + "/"
                filenameStart = word[word.rfind("/") + 1:]
            else:
                # There is no path delimiters in the string. We're only searching the current directory for the file name.
                filenameStart = word
                
        # Find all files and directories in that directory
        if os.path.isdir(directory):
            files = os.listdir(directory)
            # Find which of those files matches the end of the path
            for file in files:
                if os.path.isdir(os.path.join(directory, file)):
                    file += "/"
                if file.startswith(filenameStart):
                    self.candidates.append(file)
        return

    def getWordIdx(self) -> int:
        # Which word are we currently completing
        wordIdx = 0
        for idx in range(self.begin - 1, -1, -1):
            if self.origline[idx] == ' ':
                wordIdx += 1
        return wordIdx


###############################################################################

class Application():
    def __init__(self):
        pass

    def main(self) -> None:
        # parse command line arguments.
        arg_parser = argparse.ArgumentParser(description='Create a proxy connection.')
        arg_parser.add_argument('-b', '--bind', required=False, help='Bind IP-address for the listening socket. Default \'0.0.0.0\'', default='0.0.0.0')
        arg_parser.add_argument('-r', '--remote', required=True, help='Remote host IP-address to connect to.')
        arg_parser.add_argument('-l', '--localport', type=int, required=True, help='Local port number to bind to.')
        arg_parser.add_argument('-p', '--remoteport', type=int, required=True, help='Remote port number to connect to.')

        args = arg_parser.parse_args()

        # Setup readline
        completer = Completer()
        readline.parse_and_bind('tab: complete')
        readline.parse_and_bind('set editing-mode vi')
        readline.set_auto_history(True)
        readline.set_history_length(512)
        try:
            if os.path.exists("history.log"):
                readline.read_history_file("history.log")
        except Exception as e:
            pass

        readline.set_completer(completer.complete)

        # Create a proxy with, binding on all interfaces.
        proxy = Proxy(self, args.bind, args.remote, args.localport, args.remoteport)
        proxy.start()

        # Accept user input and parse it.
        running = True
        while running:
            try:
                reload(parser)
                try:
                    print("")
                    cmd = None
                    cmd = input('$ ')
                except KeyboardInterrupt:
                    # Allow clearing the buffer with ctrl+c
                    if not readline.get_line_buffer():
                        print("Type 'exit' or 'quit' to exit.")

                if cmd is not None:
                    running = parser.handleUserInput(cmd, proxy)

            except Exception as e:
                print('[EXCEPT] - User Input: {}'.format(e))
        
        # Save the history file.
        readline.write_history_file("history.log")
        # Kill all threads and let the OS free all resources.
        os._exit(0)

    def cmd_showhistory(self, idx: int = -1) -> None:
        if idx >= 0 and idx < readline.get_current_history_length():
            historyline = readline.get_history_item(idx)
            print(f"{idx} - {historyline}")
        elif idx == -1:
            for idx in range(0, readline.get_current_history_length()):
                historyline = readline.get_history_item(idx)
                print(f"{idx} - {historyline}")
        else:
            raise IndexError("History index out of range.")
        return

    def cmd_clearhistory(self, idx: int = -1) -> None:
        if idx >= 0 and idx < readline.get_current_history_length():
            historyline = readline.get_history_item(idx)
            # FIXME: doesn't work.
            readline.remove_history_item(idx)
            readline.write_history_file("history.log")
            print(f"Item {idx} deleted: {historyline}")
        elif idx == -1:
            readline.clear_history()
            print("History deleted.")
        else:
            raise IndexError("History index out of range.")
        return


if __name__ == '__main__':
    application = Application()
    application.main()
