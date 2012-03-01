#!/usr/bin/env python
'''
  Start and stop dbus2 servers, consumers
  Will handle remote run in the future

  bootstrap_relay  start/stop
  bootstrap_producer  start/stop
  bootstrap_server  start/stop
  bootstrap_consumer  start/stop, stop_scn, stop_after_secs
  profile_relay
  profile_consumer
  
  zookeeper  start/stop/wait_exist/wait_no_exist/wait_value/cmd
$SCRIPT_DIR/dbus2_driver.py -c zookeeper -o start --zookeeper_server_ports=${zookeeper_server_ports}  --cmdline_props="tickTime=2000;initLimit=5;syncLimit=2" --zookeeper_cmds=<semicolon separate list of command> --zookeeper_path= zookeeper_value=
  -. start, parse the port, generate the local file path in var/work/zookeeper_data/1, start, port default from 2181, generate log4j file
  -. stop, find the process id, id is port - 2181 + 1, will stop all the processes
  -. wait, query client and get the status 
  -. execute the cmd

'''
__author__ = "David Zhang"
__version__ = "$Revision: 0.1 $"
__date__ = "$Date: 2010/11/16 $"

import sys, os, fcntl
import pdb
import time, copy, re
from optparse import OptionParser, OptionGroup
import logging
import threading
import pexpect
from utility import *
import distutils.dir_util
        
# Global varaibles
options=None
server_host="localhost"
server_port="8080"
consumer_host="localhost"
consumer_port=8081
consumer_http_start_port=8081     # may need to be changed?
consumer_jmx_service_start_port=10000     # may need to be changed?
rmi_registry_port="1099"
log_file_pattern="%s_%s_%s_%s.%s.log"  # testname, component, oper, time, pid
#stats_cmd_pattern='''jps | grep %%s | awk '{printf "open "$1"\\nbean com.linkedin.databus2:relayId=1408230481,type=OutboundTrafficTotalStats\\nget *"}' | java -jar %s/../lib/jmxterm-1.0-alpha-4-uber.jar -i -n''' % get_this_file_dirname()
stats_cmd_pattern='''jps -J-Xms5M -J-Xmx5M | grep %%s | awk '{printf "open "$1"\\nbean com.linkedin.databus2:relayId=1408230481,type=OutboundTrafficTotalStats\\nget *"}' | java -jar %s/../lib/jmxterm-1.0-alpha-4-uber.jar -i -n''' % get_this_file_dirname()
#config_sub_cmd='''dbus2_config_sub.py''' % get_this_file_dirname()
jmx_cli = None

def zookeeper_opers(oper):
    if options.zookeeper_reset: zookeeper_opers_stop()
    zookeeper_setup(oper)
    globals()["zookeeper_opers_%s" % oper]()

def conf_and_deploy(ant_file):
    ''' to deploy a service only, substitue the cmd_line ops
        explored-war build-app-conf change the conf deploy.only
    '''
    conf_and_deploy_1(ant_file)

def get_stats(pattern):
    ''' called to get stats for a process '''
    pids = [x for x in sys_pipe_call_1("jps | grep %s" % pattern) if x]
    if not pids: my_error("pid for component '%s' ('%s') is not find" % (options.component, pattern))
    pid = pids[0].split()[0]
    get_stats_1(pid, options.jmx_bean, options.jmx_attr)

def wait_event(func, option=None):
    ''' called to wait for  '''
    wait_event_1(func(), option)

def producer_wait_event(name, func):
    ''' called to wait for  '''
    producer_wait_event_1(name, func())

def shutdown(oper="normal"):
    pid = send_shutdown(server_host, options.http_port or server_port, oper == "force")
    dbg_print("shutdown pid = %s" % (pid))
    ret = wait_for_condition('not process_exist(%s)' % (pid), 120)

def get_wait_timeout():
    if options.timeout: return options.timeout
    else: return 10

def pause_resume_consumer(oper):
    global consumer_port
    if options.component_id: consumer_port=find_open_port(consumer_host, consumer_http_start_port, options.component_id) 
    url = "http://%s:%s/pauseConsumer/%s" % (consumer_host, consumer_port, oper)
    out = send_url(url).split("\n")[1]
    dbg_print("out = %s" % out)
    time.sleep(0.1)

def get_bootstrap_db_conn_info():
    return ("bootstrap", "bootstrap", "bootstrap")

lock_tab_sql_file = tempfile.mkstemp()[1]
def producer_lock_tab(oper):
    dbname, user, passwd = get_bootstrap_db_conn_info()
    if oper == "lock" or oper == "save_file":
      qry = '''
drop table if exists lock_stat_tab_1;
CREATE TABLE lock_stat_tab_1 (session_id int) ENGINE=InnoDB;
drop procedure if exists my_session_wait;
delimiter $$
create procedure my_session_wait()
begin
  declare tmp int;
  LOOP
   select sleep(3600) into tmp;
  END LOOP;
end$$
delimiter ;

set @cid = connection_id();
insert into lock_stat_tab_1 values (@cid);
commit;
lock table tab_1 read local;
call my_session_wait(); 
unlock tables;
'''
      if oper == "save_file": open(lock_tab_sql_file, "w").write(qry)
      else:
        ret = mysql_exec_sql(qry, dbname, user, passwd)
        print ret
    #ret = cmd_call(cmd, options.timeout, "ERROR 2013", get_outf())
    else:
      ret = mysql_exec_sql_one_row("select session_id from lock_stat_tab_1", dbname, user, passwd)
      dbg_print(" ret = %s" % ret)
      if not ret: my_error("No lock yet")
      session_id = ret[0]
      qry = "kill %s" % session_id
      ret = mysql_exec_sql(qry, dbname, user, passwd)

def producer_purge_log():
    ''' this one is deprecated. Use the cleaner instead '''
    dbname, user, passwd = get_bootstrap_db_conn_info()
    ret = mysql_exec_sql("select id from bootstrap_sources", dbname, user, passwd, None, True)
    for srcid in [x[0] for x in ret]: # for each source
      dbg_print("srcid = %s" % srcid)
      applied_logid = mysql_exec_sql_one_row("select logid from bootstrap_applier_state", dbname, user, passwd)[0]
      qry = "select logid from bootstrap_loginfo where srcid=%s and logid<%s order by logid limit %s" % (srcid, applied_logid, options.producer_log_purge_limit)
      ret =  mysql_exec_sql(qry, dbname, user, passwd, None, True)
      logids_to_purge = [x[0] for x in ret]
      qry = ""
      for logid in logids_to_purge: qry += "drop table if exists log_%s_%s;" % (srcid, logid)
      mysql_exec_sql(qry, dbname, user, passwd)
      dbg_print("logids_to_purge = %s" % logids_to_purge)
      mysql_exec_sql("delete from bootstrap_loginfo where srcid=%s and logid in (%s); commit" % (srcid, ",".join(logids_to_purge)), dbname, user, passwd)

# load the command dictionary
parser = OptionParser(usage="usage: %prog [options]")
execfile(os.path.join(get_this_file_dirname(),"driver_cmd_dict.py"))

allowed_opers=[]
for cmd in cmd_dict: allowed_opers.extend(cmd_dict[cmd].keys())
allowed_opers=[x for x in list(set(allowed_opers)) if x!="default"]

ct=None  # global variale of the cmd thread, use to access subprocess

# need to check pid to determine if process is dead
# Thread and objects
class cmd_thread(threading.Thread):
    ''' execute one cmd in parallel, check output. there should be a timer. '''
    def __init__ (self, cmd, ret_pattern=None, outf=None):
      threading.Thread.__init__(self)
      self.daemon=True      # make it daemon, does not matter if use sys.exit()
      self.cmd = cmd
      self.ret_pattern = ret_pattern
      self.outf = sys.stdout
      if outf: self.outf = outf
      self.thread_wait_end=False
      self.thread_ret_ok=False
      self.subp=None
      self.ok_to_run=True
    def run(self):
      self.subp = subprocess_call_1(self.cmd)
      if not self.subp: 
         self.thread_wait_end=True
         return
      # capture java call here
      if options.capture_java_call: cmd_call_capture_java_call()     # test only remote
      # no block
      fd = self.subp.stdout.fileno()
      fl = fcntl.fcntl(fd, fcntl.F_GETFL)
      fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
      while (self.ok_to_run):  # for timeout case, must terminate the thread, need non block read 
        try: line = self.subp.stdout.readline()
        except IOError, e: 
          time.sleep(0.1)
          #dbg_print("IOError %s" % e)
          continue
        dbg_print("line = %s" % line)
        if not line: break
        self.outf.write("%s" % line)
        if self.ret_pattern and self.ret_pattern.search(line):
          self.thread_ret_ok=True
          break
      if not self.ret_pattern: self.thread_ret_ok=True   # no pattern ok
      self.thread_wait_end=True
      # has pattern but not find, then not ok
      #while (1):  # read the rest and close the pipe
      #  try: line = self.subp.stdout.readline()
      #  except IOError, e:
      #    break
      self.subp.stdout.close()
      dbg_print("end of thread run")

def cmd_call_capture_java_call():
    ''' this one depends on the ivy path and ps length. may not work for all '''
    short_class_name=cmd_dict[options.component]["stop"].split("grep ")[-1].split(" ")[0]
    ret = wait_for_condition('sys_pipe_call("ps -ef | grep java | grep -v grep | grep %s")' % short_class_name, 20)
    java_ps_call = sys_pipe_call("ps -ef | grep java | grep -v grep | grep %s" % short_class_name)
    #java_ps_call = tmp_str
    ivy_dir=get_ivy_dir()     # espresso has different ivy
    dbg_print("ivy_dir = %s, java_ps_call=%s" % (ivy_dir,java_ps_call))
    view_root=get_view_root()
    class_path_list = []
    #pdb.set_trace()
    for jar_path in java_ps_call.split("-classpath ")[-1].split(" com.linkedin")[0].split(":"):  # classpath
      if not jar_path: continue
      if not re.search("(%s|%s)" % (ivy_dir,view_root),jar_path): 
        class_path_list.append(jar_path)
        continue
      if re.search(ivy_dir,jar_path): 
        sub_dir= ivy_dir
        sub_str = "IVY_DIR"
      if re.search(view_root,jar_path): 
        sub_dir= view_root 
        sub_str = "VIEW_ROOT"
      class_path_list.append('"%s"' % re.sub(sub_dir,sub_str,jar_path))
    class_path_list.sort()
    class_path = "[\n      %s\n]" % "\n      ,".join(class_path_list)
    class_name = java_ps_call.split(short_class_name)[0].split(" ")[-1] + short_class_name
#cmd_direct_call={
    print '''
  ,"%s":
   {
    "class_path":%s
  ,"class_name":"%s"
   }
''' % (options.component, class_path, class_name)
#}    

    #dbg_print("class_path = %s, class_name = %s" % (class_path, class_name))
    #sys.exit(0)

def cmd_call(cmd, timeout, ret_pattern=None, outf=None):
    ''' return False if timed out. timeout is in secs '''
    #if options.capture_java_call: cmd_call_capture_java_call()     # test only remote
    global ct
    ct = cmd_thread(cmd, ret_pattern, outf)
    ct.start()
    sleep_cnt = 0
    sleep_interval = 0.5
    ret = RetCode.TIMEOUT
    while (sleep_cnt * sleep_interval < timeout):
      #if not ct.is_alive:
      if ct.thread_wait_end or (ct.subp and not process_exist(ct.subp.pid)): 
        if ct.thread_ret_ok: ret = RetCode.OK  # include find pattern or no pattern given
        else: ret= RetCode.ERROR
        #if options.capture_java_call: cmd_call_capture_java_call()
        break    # done
      time.sleep(sleep_interval)
      sleep_cnt += 1
    while (not ct.thread_wait_end):
      ct.ok_to_run = False  # terminate the thread in timeout case
      time.sleep(0.1)
    return ret

remote_component=None
remote_cmd_template='''ssh %s "bash -c 'source /export/home/eng/dzhang/bin/jdk6_env; cd %s; %s'"'''
def run_cmd_remote_setup():
    print "!!! REMOTE RUN ENABLED !!!"
    global remote_component
    component_cnt = 0
    # find the one in the cfg file, so multiple consumers must be in sequence
    for section in remote_run_config:
      if re.search(options.component, section): 
        remote_component=section
        component_cnt +=1
        if not options.component_id or compnent_cnt == options.component_id: break
    if not remote_component: my_error("No section for component %s, id %s" % (options.component, options.component_id))
    remote_component_properties = remote_run_config[remote_component]
    set_remote_view_root(remote_component_properties["view_root"])
    # create the remote var/work dir, may not be needed as the current view have them
    #sys_call("ssh %s mkdir -p %s %s" % remote_run_config[remote_component]["host"], get_remote_work_dir(), get_remote_var_dir()
   
def run_cmd_remote(cmd):
    ret = remote_cmd_template % (remote_run_config[remote_component]["host"], get_remote_view_root(),  cmd)
    return ret

metabuilder_file=".metabuilder.properties"
def get_bldfwk_dir():
    bldfwk_file = "%s/%s" % (get_view_root(), metabuilder_file)
    bldfwk_dir = None
    if not os.path.exists(bldfwk_file): return None
    for line in open(bldfwk_file):
      m = re.search("(bldshared-[0-9]+)",line)
      if m: 
        bldfwk_dir= m.group(1)
        break
    print "Warning. Cannot find bldshared-dir, run ant -f bootstrap.xml"
    #assert bldfwk_dir, "Cannot find bldshared-dir, run ant -f bootstrap.xml"
    return bldfwk_dir

rsync_path="/usr/local/bin/rsync"
remote_deploy_cmd_template='''rsync -avz  --exclude=.svn --exclude=var --exclude=mmappedBuffer --exclude=eventLog --exclude=cp_com_linkedin_events --exclude=dist --rsync-path=%s %s/ %s:%s'''
remote_deploy_bldcmd_template='''rsync -avz  --exclude=.svn --rsync-path=%s %s %s:%s'''
remote_deploy_change_blddir_cmd_template='''ssh %s "sed 's/%s/%s/' %s > %s_tmp; mv -f %s_tmp %s" '''
def do_remote_deploy():
    global rsync_path
    if not remote_run:
      print "Remote config file is not set in var/config!"
      return RetCode.ERROR;
    bldfwk_dir = get_bldfwk_dir()
    view_root = get_view_root()
    for section in remote_run_config:
      remote_host = remote_run_config[section]["host"]
      remote_view_root = remote_run_config[section]["view_root"]
      if "rsync_path" in remote_run_config[section]: rsync_path=remote_run_config[section]["rsync_path"]
      remote_view_root_parent = os.path.dirname(remote_view_root)
      sys_call("ssh %s mkdir -p %s" % (remote_host, remote_view_root))
      cmd = remote_deploy_cmd_template % (rsync_path, view_root, remote_host, remote_view_root)
      sys_call(cmd) 
      if bldfwk_dir:
        cmd = remote_deploy_bldcmd_template % (rsync_path, os.path.join(os.path.dirname(view_root),bldfwk_dir), remote_host, remote_view_root_parent)
        sys_call(cmd) 
      # replace the metabuilder, TODO, escape the /
      metabuilder_full_path = os.path.join(remote_view_root, metabuilder_file)
      cmd = remote_deploy_change_blddir_cmd_template % (remote_host, view_root.replace("/","\/"), remote_view_root.replace("/","\/"), metabuilder_full_path,  metabuilder_full_path,  metabuilder_full_path,  metabuilder_full_path)
      sys_call(cmd) 
    return 0

run_cmd_added_options=[]
def run_cmd_add_option(cmd, option_name, value=None, check_exist=False):
    global direct_java_call_jvm_args
    dbg_print("option_name = %s, value = %s" % (option_name, value))
    #option_name = option_name.split(".")[-1]  # get rid of the options., which is for readability only
    if option_name not in dir(options): my_error("invalid option name %s" % option_name)
    global run_cmd_added_options
    run_cmd_added_options.append(option_name)
    if not getattr(options, option_name): return cmd  # not such option
    if not value: value = getattr(options,option_name) 
    dbg_print("after option_name = %s, value = %s" % (option_name, value))
    if check_exist:
      full_path = file_exists(value)
      if not full_path: my_error("File does not exists! %s" % value)
      value=full_path
    if isinstance(value, str) and value[0]!='"' and not (option_name in ["cmdline_args"] and options.enable_direct_java_call):   # do not quote the cmdline args
      #value = value.replace(' ','\\ ')     # escape the white space
      value = '"%s"' % value   # quote it 
    if options.enable_direct_java_call:
      option_mapping = direct_java_call_option_mapping
      option_prefix = ""
      option_assign = ""
      if re.search("^jvm_",option_name) or option_name in direct_java_call_jvm_args:  # must start with jvm
        #pdb.set_trace()
        direct_java_call_jvm_args[option_name][1]=value  # overide the default value
        dbg_print("direct_java_call_jvm_args[%s]=%s" % (option_name,direct_java_call_jvm_args[option_name]))
        return cmd
    else:
      option_mapping = ant_call_option_mapping
      option_prefix = "-D"
      option_assign = "="
    option_mapping_name = option_name # default same as the option name
    if option_name in option_mapping: option_mapping_name = option_mapping[option_name]
    option_str = option_prefix + option_mapping_name + option_assign + value
    dbg_print("option_str = %s" % (option_str))
    if not option_str: return cmd
    cmd_split=cmd.split()
    if options.enable_direct_java_call: # add option to the end
      cmd += " %s" % option_str     
    else:
      cmd_split.insert(len(cmd_split)-1,option_str) # here it handles insert before the last one
      cmd = " ".join(cmd_split)
    dbg_print("cmd = %s" % cmd)
    return cmd
    
def run_cmd_add_log_file(cmd):
    log_file= log_file_pattern % (options.testname, options.component, options.operation, time.strftime('%y%m%d_%H%M%S'), os.getpid())
    if options.logfile: log_file = options.logfile 
    #log_file = os.path.join(remote_run and get_remote_log_dir() or get_log_dir(), log_file)
    # TODO: maybe we want to put the logs in the remote host
    log_file = os.path.join(get_log_dir(), log_file)
    dbg_print("log_file = %s" % log_file)
    open(log_file,"w").write("TEST_NAME=%s\n" % test_name) 
    # logging for all the command
    cmd += " 2>&1 | tee -a %s" % log_file 
    return cmd

def run_cmd_get_return_pattern():
    ret_pattern = None
    pattern_key = "%s_%s" % (options.component, options.operation)
    if pattern_key in cmd_ret_pattern: ret_pattern = cmd_ret_pattern[pattern_key]
    dbg_print("ret_pattern = %s" % ret_pattern)
    return ret_pattern

def run_cmd_setup():
    if re.search("_consumer",options.component): 
      global consumer_host
      if remote_run: consumer_host = remote_component_properties["host"]
      else: consumer_host = "localhost"
      dbg_print("consumer_host= %s" % consumer_host)

def run_cmd_add_config(cmd):
    if options.operation in ["start","clean_log","default"]: 
      pass_down_options=ant_call_option_mapping.keys()
      if options.config: 
        if not remote_run: 
          cmd = run_cmd_add_option(cmd, "config", options.config, check_exist=True)      # check exist will figure out
        else: 
          cmd = run_cmd_add_option(cmd, "config", os.path.join(get_remote_view_root(), options.config), check_exist=False)  
      run_cmd_view_root = remote_run and get_remote_view_root() or get_view_root()
      cmd = run_cmd_add_option(cmd, "dump_file", options.dump_file and os.path.join(run_cmd_view_root, options.dump_file) or None)
      cmd = run_cmd_add_option(cmd, "value_file", options.value_file and os.path.join(run_cmd_view_root, options.value_file) or None)
      cmd = run_cmd_add_option(cmd, "log4j_file", options.log4j_file and os.path.join(run_cmd_view_root, options.log4j_file) or None)
      cmd = run_cmd_add_option(cmd, "jvm_direct_memory_size")
      cmd = run_cmd_add_option(cmd, "jvm_max_heap_size")
      cmd = run_cmd_add_option(cmd, "jvm_gc_log")
      cmd = run_cmd_add_option(cmd, "jvm_args")
      cmd = run_cmd_add_option(cmd, "db_config_file")
      cmd = run_cmd_add_option(cmd, "cmdline_props")
      cmd = run_cmd_add_option(cmd, "filter_conf_file")

      if options.checkpoint_dir: 
         if options.checkpoint_dir == "auto":
           checkpoint_dir = os.path.join(get_work_dir(), "databus2_checkpoint_%s_%s" % time.strftime('%y%m%d_%H%M%S'), os.getpid())
         else:
           checkpoint_dir = options.checkpoint_dir
         checkpoint_dir = os.path.join(run_cmd_view_root(), checkpoint_dir) 
         cmd = run_cmd_add_option(cmd, "checkpoint_dir", checkpoint_dir)   
         # clear up the directory
         if not options.checkpoint_keep and os.path.exists(checkpoint_dir): distutils.dir_util.remove_tree(checkpoint_dir)

      # options can be changed during remote run
      if remote_run: 
        remote_component_properties = remote_run_config[remote_component]
        if not options.relay_host and "relay_host" in remote_component_properties: options.relay_host = remote_component_properties["relay_host"]
        if not options.relay_port and "relay_port" in remote_component_properties: options.relay_port = remote_component_properties["relay_port"]
        if not options.bootstrap_host and "bootstrap_host" in remote_component_properties: options.bootstrap_host = remote_component_properties["bootstrap_host"]
        if not options.bootstrap_port and "bootstrap_port" in remote_component_properties: options.bootstrap_port = remote_component_properties["bootstrap_port"]
      cmd = run_cmd_add_option(cmd, "relay_host")
      cmd = run_cmd_add_option(cmd, "relay_port")
      cmd = run_cmd_add_option(cmd, "bootstrap_host")
      cmd = run_cmd_add_option(cmd, "bootstrap_port")
      cmd = run_cmd_add_option(cmd, "consumer_event_pattern")
      if re.search("_consumer",options.component): 
        # next available port
        if options.http_port: http_port = options.http_port
        else: http_port = next_available_port(consumer_host, consumer_http_start_port)   
        cmd = run_cmd_add_option(cmd, "http_port", http_port)
        cmd = run_cmd_add_option(cmd, "jmx_service_port", next_available_port(consumer_host, consumer_jmx_service_start_port))   
      for option in [x for x in pass_down_options if x not in run_cmd_added_options]:
        cmd = run_cmd_add_option(cmd, option)
        
    if options.enable_direct_java_call: 
      cmd = re.sub("java -classpath","java -d64 -ea %s -classpath" % " ".join([x[0]+x[1] for x in direct_java_call_jvm_args.values() if x[1]]) ,cmd) # d64 here
    dbg_print("cmd = %s" % cmd)
    return cmd

def run_cmd_add_ant_debug(cmd): 
    if re.search("^ant", cmd): cmd = re.sub("^ant","ant -d", cmd)
    dbg_print("cmd = %s" % cmd)
    return cmd

def run_cmd_direct_java_call(cmd, component): 
    ''' this needs to be consistent with adding option 
        currently ant -f ; will mess up if there are options
    '''
    if not component in cmd_direct_call:
      options.enable_direct_java_call = False   # disable direct java call if classpath not given
      return cmd
    if re.search("^ant", cmd): # only component in has class path given will be 
      ivy_dir = get_ivy_dir()
      view_root = get_view_root()
      class_path_list=[]
      for class_path in cmd_direct_call[component]["class_path"]:
        if re.search("IVY_DIR",class_path): 
          class_path_list.append(re.sub("IVY_DIR", ivy_dir,class_path))
          continue
        if re.search("VIEW_ROOT",class_path): 
          class_path_list.append(re.sub("VIEW_ROOT", view_root,class_path))
          if not os.path.exists(class_path_list[-1]): # some jars not in VIEW_ROOT, trigger before command
            if "before_cmd" in cmd_direct_call[component]: 
              before_cmd = "%s; " % cmd_direct_call[component]["before_cmd"]
              sys_call(before_cmd)
          continue
        class_path_list.append(class_path)
      if options.check_class_path: 
        for jar_file in class_path_list: 
          if not os.path.exists(jar_file): 
            print "==WARNING NOT EXISTS: " + jar_file
            new_jar_path = sys_pipe_call("find %s -name %s" % (ivy_dir, os.path.basename(jar_file))).split("\n")[0]
            if new_jar_path: 
              print "==found " + new_jar_path
            class_path_list[class_path_list.index(jar_file)] = new_jar_path
      direct_call_cmd = "java -classpath %s %s" % (":".join(class_path_list), cmd_direct_call[component]["class_name"])
      if re.search("^ant.*;",cmd): cmd = re.sub("^ant.*;","%s" % direcdt_call_cmd, cmd)
      else: cmd = re.sub("^ant.*$",direct_call_cmd, cmd)
    dbg_print("cmd = %s" % cmd)
    return cmd

def run_cmd():
    if (options.component=="bootstrap_dbreset"): setup_rmi("stop")
    if (not options.operation): options.operation="default"
    if (not options.testname): 
      options.testname = "TEST_NAME" in os.environ and os.environ["TEST_NAME"] or "default"
    if (options.operation not in cmd_dict[options.component]): 
      my_error("%s is not one of the command for %s. Valid values are %s " % (options.operation, options.component, cmd_dict[options.component].keys()))
    # handle the different connetion string for hudson
    if (options.component=="db_relay" and options.db_config_file): 
       options.db_config_file = db_config_change(options.db_config_file)
    if (options.component=="test_bootstrap_producer" and options.operation=="lock_tab"): 
      producer_lock_tab("save_file")
    cmd = cmd_dict[options.component][options.operation]  
    # cmd can be a funciton call
    if isinstance(cmd, list): 
      if not callable(cmd[0]): my_error("First element should be function")
      cmd[0](*tuple(cmd[1:]))        # call the function
      return
    if options.enable_direct_java_call: cmd = run_cmd_direct_java_call(cmd, options.component)
    if remote_run: run_cmd_remote_setup()
    if options.ant_debug: cmd = run_cmd_add_ant_debug(cmd) # need ant debug call or not
    cmd = run_cmd_add_config(cmd) # handle config file
    if remote_run: cmd = run_cmd_remote(cmd) 
    ret_pattern = run_cmd_get_return_pattern()
    cmd = run_cmd_add_log_file(cmd)
    ret = cmd_call(cmd, options.timeout, ret_pattern, get_outf())
    if options.operation == "stop": time.sleep(0.1)
    return ret

def setup_rmi_cond(oper):
    rmi_up = isOpen(server_host, rmi_registry_port)
    dbg_print("rmi_up = %s" % rmi_up)
    if oper=="start": return rmi_up
    if oper=="stop": return not rmi_up

def setup_rmi(oper="start"):
    ''' start rmi registry if not alreay started '''
    ret = RetCode.OK
    dbg_print("oper = %s" % oper)
    rmi_up = isOpen(server_host, rmi_registry_port)
    rmi_str = "ant -f sitetools/rmiscripts/build.xml; ./rmiservers/bin/rmiregistry%s" % oper
    if oper=="stop": sys_call(kill_cmd_template % "RegistryImpl")  # make sure it stops
    if (oper=="start" and not rmi_up) or (oper=="stop" and rmi_up):
      sys_call(rmi_str)
      # wait for rmi
      ret = wait_for_condition('setup_rmi_cond("%s")' % oper)

def setup_env():
    setup_work_dir()
    #setup_rmi()

def get_outf():
    outf = sys.stdout
    if options.output: outf = open(options.output,"w")
    return outf

def start_jmx_cli():
    global jmx_cli
    if not jmx_cli:
      jmx_cli = pexpect.spawn("java -jar %s/../lib/jmxterm-1.0-alpha-4-uber.jar" % get_this_file_dirname())
      jmx_cli.expect("\$>")

def stop_jmx_cli():
    global jmx_cli
    if jmx_cli:
      jmx_cli.sendline("quit")
      jmx_cli.expect(pexpect.EOF)
      jmx_cli = None

def jmx_cli_cmd(cmd):
    if not jmx_cli: start_jmx_cli()
    dbg_print("jmx cmd = %s" % cmd)
    jmx_cli.sendline(cmd)
    jmx_cli.expect("\$>")
    ret = jmx_cli.before.split("\r\n")[1:]
    dbg_print("jmx cmd ret = %s" % ret)
    return ret

def get_stats_1(pid, jmx_bean, jmx_attr):
    outf = get_outf()
    start_jmx_cli()
    jmx_cli_cmd("open %s" % pid)
    ret = jmx_cli_cmd("beans")
    if jmx_bean=="list": 
      stat_re = re.compile("^com.linkedin.databus2:")
      stats = [x for x in ret if stat_re.search(x)]
      outf.write("%s\n" % "\n".join(stats))
      return
    stat_re = re.compile("^com.linkedin.databus2:.*%s$" % jmx_bean)
    stats = [x for x in ret if stat_re.search(x)]
    if not stats: # stats not find
      stat_re = re.compile("^com.linkedin.databus2:")
      stats = [x.split("=")[-1].rstrip() for x in ret if stat_re.search(x)]
      my_error("Possible beans are %s" % stats)
    full_jmx_bean = stats[0] 
    jmx_cli_cmd("bean %s" % full_jmx_bean)
    if jmx_attr == "all": jmx_attr = "*"
    ret = jmx_cli_cmd("get %s" % jmx_attr)
    outf.write("%s\n" % "\n".join(ret))
    stop_jmx_cli()

def run_testcase(testcase):
    dbg_print("testcase = %s" % testcase)
    os.chdir(get_testcase_dir()) 
    if not re.search("\.test$", testcase): testcase += ".test"
    if not os.path.exists(testcase): 
      my_error("Test case %s does not exist" % testcase)
    dbg_print("testcase = %s" % testcase)
    ret = sys_call("/bin/bash %s" % testcase)
    os.chdir(view_root)
    return ret

def get_ebuf_inbound_total_maxStreamWinScn(host, port, option=None):
    url_template = "http://%s:%s/containerStats/inbound/events/total"    
    if option == "bootstrap":
       url_template = "http://%s:%s/clientStats/bootstrap/events/total"
    return http_get_field(url_template, host, port, "maxSeenWinScn")

def consumer_reach_maxStreamWinScn(maxWinScn, host, port, option=None):
    consumerMaxWinScn = get_ebuf_inbound_total_maxStreamWinScn(host, port, option)
    dbg_print("consumerMaxWinScn = %s, maxWinScn = %s" % (consumerMaxWinScn, maxWinScn))
    return consumerMaxWinScn >= maxWinScn

def producer_reach_maxStreamWinScn(name, maxWinScn):
    ''' select max of all the sources '''
    dbname, user, passwd = get_bootstrap_db_conn_info()
    tab_name = (name == "producer") and "bootstrap_producer_state" or "bootstrap_applier_state"
    qry = "select max(windowscn) from %s " % tab_name
    ret = mysql_exec_sql_one_row(qry, dbname, user, passwd)
    producerMaxWinScn = ret and ret[0] or 0   # 0 if no rows
    dbg_print("producerMaxWinScn = %s, maxWinScn = %s" % (producerMaxWinScn, maxWinScn))
    return producerMaxWinScn >= maxWinScn

def wait_for_condition(cond, timeout=60, sleep_interval = 0.1):
    ''' wait for a certain cond. cond could be a function. 
       This cannot be in utility. Because it needs to see the cond function '''
    dbg_print("cond = %s" % cond)
    sleep_cnt = 0
    ret = RetCode.TIMEOUT
    while (sleep_cnt * sleep_interval < timeout):
      if eval(cond): 
        ret = RetCode.OK
        break
      time.sleep(sleep_interval)
      sleep_cnt += 1
    return ret

def producer_wait_event_1(name, timeout):
    ''' options.relay_host should be set for remote_run '''
    relay_host = options.relay_host and options.relay_host or server_host
    relay_port = options.relay_port and options.relay_port or server_port
    if options.sleep_before_wait: time.sleep(options.sleep_before_wait)
    maxWinScn = get_ebuf_inbound_total_maxStreamWinScn(relay_host, relay_port)
    dbg_print("maxWinScn = %s, timeout = %s" % (maxWinScn, timeout))
    ret = wait_for_condition('producer_reach_maxStreamWinScn("%s", %s)' % (name,maxWinScn), timeout)
    if ret == RetCode.TIMEOUT: print "Timed out waiting consumer to reach maxWinScn %s" % maxWinScn
    return ret

def send_shutdown(host, port, force=False):
    ''' use kill which is much faster '''
    #url_template = "http://%s:%s/operation/shutdown" 
    url_template = "http://%s:%s/operation/getpid" 
    pid = http_get_field(url_template, host, port, "pid")
    force_str = force and "-9" or ""
    sys_call("kill %s %s" % (force_str,pid))
    return pid

def wait_event_1(timeout, option=None):
    relay_host = options.relay_host and options.relay_host or server_host
    relay_port = options.relay_port and options.relay_port or server_port
    maxWinScn = get_ebuf_inbound_total_maxStreamWinScn(relay_host, relay_port)
    print "Wait maxWinScn:%s" % maxWinScn
    dbg_print("maxWinScn = %s, timeout = %s" % (maxWinScn, timeout))
    # consumer host is defined already
    global consumer_port
    if options.component_id: consumer_port=find_open_port(consumer_host, consumer_http_start_port, options.component_id) 
    if options.http_port: consumer_port = options.http_port
    ret = wait_for_condition('consumer_reach_maxStreamWinScn(%s, "%s", %s, "%s")' % (maxWinScn, consumer_host, consumer_port, option and option or ""), timeout)
    if ret == RetCode.TIMEOUT: print "Timed out waiting consumer to reach maxWinScn %s" % maxWinScn
    if options.sleep_after_wait: time.sleep(options.sleep_after_wait)
    return ret

def conf_and_deploy_1_find_dir_name(ant_target, screen_out):
    found_target = False
    copy_file_re = re.compile("\[copy\] Copying 1 file to (.*)")
    for line in screen_out:
      if not found_target and line == ant_target: found_target = True
      if found_target:
         dbg_print("line = %s" % line)
         m = copy_file_re.search(line) 
         if m: return m.group(1)
    return None

def conf_and_deploy_1_find_extservice(dir_name):
    extservice_re = re.compile("extservices.*\.springconfig")
    flist = os.listdir(dir_name)
    flist.sort(reverse=True)
    for fname in flist:
      if extservice_re.search(fname): return os.path.join(dir_name, fname)
    return None

def conf_and_deploy_1_find_extservice_name(ant_target, screen_out):
    found_target = False
    copy_file_re = re.compile("\[copy\] Copying (\S*) to ")
    for line in screen_out:
      if not found_target and line == ant_target: found_target = True
      if found_target:
         dbg_print("line = %s" % line)
         m = copy_file_re.search(line) 
         if m: return m.group(1)
    return None


from xml.dom.minidom import parse
def conf_and_deploy_1_add_conf(file_name):
    dom1 = parse(file_name)
    for prop in options.extservice_props: 
      props = prop.split(";")
      len_props = len(props)
      if len_props not in (2,3): 
        print "WARNING: prop %s is not a valid setting. IGNORED" % prop
        continue
      is_top_level= (len_props == 2)
      find_keys=[x for x in dom1.getElementsByTagName("entry") if x.attributes["key"].value == props[0]]
      dbg_print("find_keys = %s" % find_keys)
      if not find_keys: 
        print "WARNING: prop %s part %s is not in file %s. IGNORED" % (prop, props[0], file_name)
        continue
      keyNode = find_keys[0] 
      if is_top_level: 
        keyNode.attributes["value"].value=props[-1]
        continue
      find_props= [x for x in keyNode.getElementsByTagName("prop") if x.attributes["key"].value == props[1]]
      dbg_print("find_props = %s" % find_props)
      if not find_props: 
        print "WARNING: prop %s part %s is not in file %s. IGNORED" % (prop, props[1], file_name)
        continue
      find_props[0].childNodes[0].nodeValue=props[-1]
    open(file_name,"w").write(dom1.toxml())

def conf_and_deploy_1(ant_file):
    ''' to deploy a service only, do exploded-war first,
        then build-app-conf substitute the extservice_props into the extservice file
        the deploy.only.noconf to deploy the service using the new conf
    '''
    #pdb.set_trace()
    #out = sys_pipe_call("ant -f %s build-app-conf" % (ant_file))
    #dir_name = conf_and_deploy_1_find_dir_name("build-app-conf:", out.split("\n"))
    tmp_file = tempfile.mkstemp()[1]
    cmd = "ant -f %s exploded-war 2>&1 | tee %s" % (ant_file, tmp_file)
    ret = cmd_call(cmd, 60, re.compile("BUILD SUCCESSFUL"))
    cmd = "ant -f %s build-app-conf 2>&1 | tee %s" % (ant_file, tmp_file)
    ret = cmd_call(cmd, 5, re.compile("BUILD SUCCESSFUL"))
    dir_name = conf_and_deploy_1_find_dir_name("build-app-conf:", [x.rstrip() for x in open(tmp_file).readlines()])
    dbg_print("dir_name = %s" % dir_name)
    if dir_name: extservice_file_name = conf_and_deploy_1_find_extservice(dir_name)
    if not dir_name or not extservice_file_name: my_error("No extservice file in dir %s" % dir_name)
    #out = sys_pipe_call("ant -f %s -d build-app-conf" % (ant_file))
    #extservice_file_name = conf_and_deploy_1_find_extservice_name("build-app-conf:", out.split("\n"))
    dbg_print("extservice_file_name = %s" % extservice_file_name)
    if options.extservice_props: 
      tmp_files = [extservice_file_name]
      tmp_files = save_copy([extservice_file_name])
      dbg_print("new_files = %s" % tmp_files)
      conf_and_deploy_1_add_conf(extservice_file_name)
      #shutil.copy(tmp_files[0], extservice_file_name)
    # do the deploy
    #pdb.set_trace()
    cmd = "ant -f %s deploy.only.noconf 2>&1 | tee %s" % (ant_file, tmp_file)
    ret = cmd_call(cmd, 60, re.compile("BUILD SUCCESSFUL"))

zookeeper_cmd=None
zookeeper_server_ports=None
zookeeper_server_dir=None
zookeeper_server_ids=None

#possible_ivy_dir=[os.path.join(os.environ["HOME"],".ivy2/lin-cache/ivy-cache"),os.path.join(os.environ["HOME"],".ivy2/lin-cache"),"/ivy/.ivy2/ivy-cache","/ivy/.ivy2"]
possible_ivy_dir=[os.path.join(os.environ["HOME"],".m2/repository"), os.path.join(os.environ["HOME"],".ivy2/lin-cache/"),"/ivy/.ivy2"]
def get_ivy_dir():
    for ivy_dir in possible_ivy_dir:
      if os.path.exists(ivy_dir): break
    if not os.path.exists(ivy_dir): raise
    return ivy_dir
 
def zookeeper_setup(oper):
    ''' may need to do a find later. find $HOME/.ivy2/lin-cache -name zookeeper-3.3.0.jar '''
    global zookeeper_cmd, zookeeper_server_ports, zookeeper_server_dir, zookeeper_server_ids
    #possible_ivy_home_dir=[os.path.join(os.environ["HOME"],".ivy2/lin-cache/"),"/ivy/.ivy2"]
    possible_ivy_home_dir=[os.path.join(os.environ["HOME"],".m2/repository/"), os.path.join(os.environ["HOME"],".ivy2/lin-cache/"),"/ivy/.ivy2"]
    ivy_dir = get_ivy_dir()
    zookeeper_class= (oper=="start") and  "org.apache.zookeeper.server.quorum.QuorumPeerMain" or "org.apache.zookeeper.ZooKeeperMain"
    log4j_file=os.path.join(get_view_root(),"integration-test/config/zookeeper-log4j2file.properties")
    # REMOVE
    #ivy_dir=os.path.join(os.environ["HOME"],".ivy2/lin-cache")
    #zookeeper_classpath="%s/org.apache.zookeeper/zookeeper/3.3.0/zookeeper-3.3.0.jar:%s/log4j/log4j/1.2.15/log4j-1.2.15.jar %s" % (ivy_dir, ivy_dir)
    zookeeper_classpath="%s/org/apache/zookeeper/zookeeper/3.3.0/zookeeper-3.3.0.jar:%s/log4j/log4j/1.2.15/log4j-1.2.15.jar" % (ivy_dir, ivy_dir)
    zookeeper_cmd="java -d64 -Xmx512m -Dlog4j.configuration=file://%s -cp %s %s" % (log4j_file, zookeeper_classpath, zookeeper_class)
    dbg_print("zookeeper_cmd=%s" % (zookeeper_cmd))
    zookeeper_server_ports= options.zookeeper_server_ports and options.zookeeper_server_ports or "localhost:2181"
    zookeeper_server_dir=os.path.join(get_work_dir(),"zookeeper_data")
    zookeeper_server_ids= options.zookeeper_server_ids and [int(x) for x in options.zookeeper_server_ids.split(",")] or range(1,len(zookeeper_server_ports.split(","))+1)
    dbg_print("zookeeper_server_ids=%s" % (zookeeper_server_ids))

def zookeeper_opers_start_create_conf(zookeeper_server_ports_split):
    zookeeper_num_servers = len(zookeeper_server_ports_split)
    zookeeper_server_conf_files=[]
    zookeeper_internal_port_1_start = 2800
    zookeeper_internal_port_2_start = 3800
    # overide the default config
    server_conf={"tickTime":2000,"initLimit":5,"syncLimit":2}
    if options.cmdline_props:
      for pair in options.cmdline_props.split(";"):
        (k, v) = pair.split("=")
        if k in server_conf: server_conf[k] = v
    # get the server
    zookeeper_internal_conf=""
    for k in server_conf: zookeeper_internal_conf+="%s=%s\n" % (k, server_conf[k])
    dbg_print("zookeeper_internal_conf = %s" % zookeeper_internal_conf)
    for server_id in range(1,zookeeper_num_servers+1):
      zookeeper_host = zookeeper_server_ports_split[server_id-1].split(":")[0]
      zookeeper_internal_port_1 = zookeeper_internal_port_1_start + server_id -1 
      zookeeper_internal_port_2 = zookeeper_internal_port_2_start +  server_id -1
      zookeeper_internal_conf += "server.%s=%s:%s:%s\n" % (server_id, zookeeper_host, zookeeper_internal_port_1, zookeeper_internal_port_2) 
    dbg_print("zookeeper_internal_conf = %s" % zookeeper_internal_conf)

    for server_id in range(1,zookeeper_num_servers+1):
      if server_id not in zookeeper_server_ids: continue
      conf_file = os.path.join(zookeeper_server_dir,"conf_%s" % server_id)
      dataDir=os.path.join(zookeeper_server_dir,str(server_id))
      zookeeper_port = zookeeper_server_ports_split[server_id-1].split(":")[1]
      conf_file_p = open(conf_file, "w")
      conf_file_p.write("clientPort=%s\n" % zookeeper_port)
      conf_file_p.write("dataDir=%s\n" % dataDir)
      conf_file_p.write("%s\n" % zookeeper_internal_conf)
      conf_file_p.close()
      dbg_print("==conf file %s: \n %s" % (conf_file, open(conf_file).readlines()))
      zookeeper_server_conf_files.append(conf_file)
    return zookeeper_server_conf_files

def zookeeper_opers_start_create_dirs(zookeeper_server_ports_split):
    for server_id in range(1,len(zookeeper_server_ports_split)+1):
      if server_id not in zookeeper_server_ids: continue
      current_server_dir=os.path.join(zookeeper_server_dir,str(server_id))
      dbg_print("current_server_dir = %s" % current_server_dir)
      if os.path.exists(current_server_dir): 
        if not options.zookeeper_reset: continue
        distutils.dir_util.remove_tree(current_server_dir)
      distutils.dir_util.mkpath(current_server_dir)
      my_id_file=os.path.join(current_server_dir, "myid")
      open(my_id_file,"w").write("%s\n" % server_id)
    
def zookeeper_opers_start():
    zookeeper_server_ports_split = zookeeper_server_ports.split(",")
    zookeeper_opers_start_create_dirs(zookeeper_server_ports_split)
    conf_files = zookeeper_opers_start_create_conf(zookeeper_server_ports_split)
    for conf_file in conf_files:
      # no log file for now
      #cmd = run_cmd_add_log_file(cmd)
      ret = cmd_call("%s %s" % (zookeeper_cmd, conf_file), 60, re.compile("My election bind port"))
    
def zookeeper_opers_stop():
    # may be better to use pid, but somehow it is not in the datadir
    sys_call(kill_cmd_template % "QuorumPeerMain")

def zookeeper_opers_wait_for_exist():
    pass
def zookeeper_opers_wait_for_nonexist():
    pass
def zookeeper_opers_wait_for_value():
    pass
def zookeeper_opers_cmd():
    if not options.zookeeper_cmds: 
      print "No zookeeper_cmds given"
      return
    splitted_cmds = ";".join(["echo %s" % x for x in options.zookeeper_cmds.split(";")])
    sys_call("(%s) | %s -server %s" % (splitted_cmds, zookeeper_cmd, zookeeper_server_ports))
 
def main(argv):
    # default 
    global options
    parser.add_option("-n", "--testname", action="store", dest="testname", default="default", help="A test name identifier")
    parser.add_option("-c", "--component", action="store", dest="component", default=None, choices=cmd_dict.keys(),
                       help="%s" % cmd_dict.keys())
    parser.add_option("-o", "--operation", action="store", dest="operation", default=None, choices=allowed_opers,
                       help="%s" % allowed_opers)
    parser.add_option("", "--output", action="store", dest="output", default=None,
                       help="Output file name. Default to stdout")
    parser.add_option("", "--logfile", action="store", dest="logfile", default=None,
                       help="log file for both stdout and stderror. Default auto generated")
    parser.add_option("","--timeout", action="store", type="long", dest="timeout", default=600,
                       help="Time out in secs before waiting for the success pattern. [default: %default]")

    jvm_group = OptionGroup(parser, "jvm options", "")
    jvm_group.add_option("", "--jvm_direct_memory_size", action="store", dest="jvm_direct_memory_size", default = None,
                       help="Set the jvm direct memory size. e.g., 2048m. Default using the one in ant file.")
    jvm_group.add_option("", "--jvm_max_heap_size", action="store", dest="jvm_max_heap_size", default = None,
                       help="Set the jvm head size. e.g., 1024m. Default using the one in ant file.")
    jvm_group.add_option("", "--jvm_args", action="store", dest="jvm_args", default = None,
                       help="Other jvm args. e.g., '-Xms24m -Xmx50m'")
    jvm_group.add_option("", "--jvm_gc_log", action="store", dest="jvm_gc_log", default = None,
                       help="Enable gc and give jvm gc log file")

    test_case_group = OptionGroup(parser, "Testcase options", "")
    test_case_group.add_option("", "--testcase", action="store", dest="testcase", default = None,
                       help="Run a test. Report error. Default no test")

    stats_group = OptionGroup(parser, "Stats options", "")
    stats_group.add_option("","--jmx_bean", action="store", dest="jmx_bean", default="list",
                       help="jmx bean to get. [default: %default]")
    stats_group.add_option("","--jmx_att", action="store", dest="jmx_attr", default="all",
                       help="jmx attr to get. [default: %default]")

    remote_group = OptionGroup(parser, "Remote options", "")
    remote_group.add_option("", "--remote_run", action="store_true", dest="remote_run", default = False,
                       help="Run remotely based on config file. Default False")
    remote_group.add_option("", "--remote_deploy", action="store_true", dest="remote_deploy", default = False,
                       help="Deploy the source tree to the remote machine based on config file. Default False")
    remote_group.add_option("", "--remote_config_file", action="store", dest="remote_config_file", default = None,
                       help="Remote config file")

    zookeeper_group = OptionGroup(parser, "Zookeeper options", "")
    zookeeper_group.add_option("", "--zookeeper_server_ports", action="store", dest="zookeeper_server_ports", default = None,
                       help="comma separated zookeeper ports, used to start/stop/connect to zookeeper")
    zookeeper_group.add_option("", "--zookeeper_path", action="store", dest="zookeeper_path", default = None,
                       help="the zookeeper path to wait for")
    zookeeper_group.add_option("", "--zookeeper_value", action="store", dest="zookeeper_value", default = None,
                       help="zookeeper path value")
    zookeeper_group.add_option("", "--zookeeper_cmds", action="store", dest="zookeeper_cmds", default = None,
                       help="cmds to send to zookeeper client. Comma separated ")
    zookeeper_group.add_option("", "--zookeeper_server_ids", action="store", dest="zookeeper_server_ids", default = None,
                       help="Comma separated list of server to start. If not given, start the number of servers in zookeeper_server_ports. This is used to start server on multiple machines ")
    zookeeper_group.add_option("", "--zookeeper_reset", action="store_true", dest="zookeeper_reset", default = False,
                       help="If true recreate server dir, otherwise start from existing server dir")


    debug_group = OptionGroup(parser, "Debug options", "")
    debug_group.add_option("-d", "--debug", action="store_true", dest="debug", default = False,
                       help="debug mode")
    debug_group.add_option("--ant_debug", action="store_true", dest="ant_debug", default = False,
                       help="ant debug mode")
    debug_group.add_option("--capture_java_call", action="store_true", dest="capture_java_call", default = False,
                       help="capture the java call")
    debug_group.add_option("--enable_direct_java_call", action="store_true", dest="enable_direct_java_call", default = True,
    #debug_group.add_option("--enable_direct_java_call", action="store_true", dest="enable_direct_java_call", default = False,
                       help="enable direct java call. ")
    debug_group.add_option("--check_class_path", action="store_true", dest="check_class_path", default = True,
                       help="check if class path exists. ")
    debug_group.add_option("", "--sys_call_debug", action="store_true", dest="enable_sys_call_debug", default = False,
                       help="debug sys call")

    # load local options
    #execfile(os.path.join(get_this_file_dirname(),"driver_local_options.py"))
    #pdb.set_trace()
   
    parser.add_option_group(jvm_group)
    parser.add_option_group(config_group)
    parser.add_option_group(other_option_group)
    parser.add_option_group(test_case_group)
    parser.add_option_group(stats_group)
    parser.add_option_group(remote_group)
    parser.add_option_group(zookeeper_group)
    parser.add_option_group(debug_group)

    (options, args) = parser.parse_args()
    set_debug(options.debug)
    set_sys_call_debug(options.enable_sys_call_debug)
    dbg_print("options = %s  args = %s" % (options, args))

    arg_error=False
    if not options.component and not options.testcase and not options.remote_deploy:
       print("\n!!!Please give component!!!\n")
       arg_error=True
    if arg_error: 
      parser.print_help()
      parser.exit()

    setup_env()
    if options.testcase:
      ret = run_testcase(options.testcase)
      if ret!=0: ret=1     # workaround a issue that ret of 256 will become 0 after sys.exit
      sys.exit(ret)
    if options.remote_deploy or options.remote_run:
      if options.remote_config_file:
        remote_config_file = file_exists(options.remote_config_file)
        if not remote_config_file: my_error("remote_config_file %s does not existi!!" % options.remote_config_file)
        dbg_print("remote_config_file = %s" % remote_config_file)
        global remote_run, remote_config
        remote_config = parse_config(remote_config_file)
        remote_run=True
      if options.remote_deploy: 
        sys_call_debug_begin()
        ret = do_remote_deploy()
        sys_call_debug_end()
        sys.exit(ret)
    sys_call_debug_begin()
    ret = run_cmd()
    sys_call_debug_end()
    sys.exit(ret)
    
if __name__ == "__main__":
    main(sys.argv[1:])

