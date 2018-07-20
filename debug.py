import subprocess

def print_debug(text):
    print('< -------------------' + text +  '------------------- >')
    print(subprocess.check_output(['nvidia-smi']))
    proc1 = subprocess.Popen(['top', '-b', '-n', '1'], stdout=subprocess.PIPE)
    proc2 = subprocess.Popen(['grep', 'pm574'], stdin=proc1.stdout,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc1.stdout.close() # Allow proc1 to receive a SIGPIPE if proc2 exits.
    out, err = proc2.communicate()
    print('out-top:\n{0}'.format(out))
