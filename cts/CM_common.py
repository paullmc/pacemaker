""" Cluster Manager common class for Pacemaker's Cluster Test Suite (CTS)

This was originally the cluster manager class for the Heartbeat stack.
It is retained for use as a base class by other cluster manager classes.
It could be merged into the ClusterManager class directly, but this is
easier.
"""

__copyright__ = """Original Author: Huang Zhen <zhenhltc@cn.ibm.com>
Copyright 2004 International Business Machines

with later changes copyright 2004-2020 the Pacemaker project contributors.
The version control history for this file may have further details.
"""
__license__ = "GNU General Public License version 2 or later (GPLv2+) WITHOUT ANY WARRANTY"

import sys
from cts.CTSvars  import *
from cts.CTS      import *
from cts.CIB      import *
from cts.CTStests import AuditResource
from cts.watcher  import LogWatcher

class crm_common(ClusterManager):
    def __init__(self, Environment, randseed=None, name=None):
        ClusterManager.__init__(self, Environment, randseed=randseed)

        self.fastfail = 0
        self.cib_installed = 0
        self.config = None
        self.cluster_monitor = 0
        self.use_short_names = 1

        if self.Env["DoBSC"]:
            del self.templates["Pat:They_stopped"]

        self._finalConditions()

        self.check_transitions = 0
        self.check_elections = 0
        self.CIBsync = {}
        self.CibFactory = ConfigFactory(self)
        self.cib = self.CibFactory.createConfig(self.Env["Schema"])
    
    def errorstoignore(self):
        # At some point implement a more elegant solution that 
        #   also produces a report at the end
        """ Return a list of known error messages that should be ignored """
        return PatternSelector().get_patterns(self.name, "BadNewsIgnore")

    def install_config(self, node):
        if not self.ns.WaitForNodeToComeUp(node):
            self.log("Node %s is not up." % node)
            return None

        if not node in self.CIBsync and self.Env["ClobberCIB"] == 1:
            self.CIBsync[node] = 1
            self.rsh(node, "rm -f "+CTSvars.CRM_CONFIG_DIR+"/cib*")

            # Only install the CIB on the first node, all the other ones will pick it up from there
            if self.cib_installed == 1:
                return None

            self.cib_installed = 1
            if self.Env["CIBfilename"] == None:
                self.log("Installing Generated CIB on node %s" % (node))
                self.cib.install(node)

            else:
                self.log("Installing CIB (%s) on node %s" % (self.Env["CIBfilename"], node))
                if 0 != self.rsh.cp(self.Env["CIBfilename"], "root@" + (self.templates["CIBfile"] % node)):
                    raise ValueError("Can not scp file to %s %d"%(node))
        
            self.rsh(node, "chown "+CTSvars.CRM_DAEMON_USER+" "+CTSvars.CRM_CONFIG_DIR+"/cib.xml")

    def prepare(self):
        '''Finish the Initialization process. Prepare to test...'''

        self.partitions_expected = 1
        for node in self.Env["nodes"]:
            self.ShouldBeStatus[node] = ""
            if self.Env["experimental-tests"]:
                self.unisolate_node(node)
            self.StataCM(node)

    def test_node_CM(self, node):
        '''Report the status of the cluster manager on a given node'''

        watchpats = [ ]
        watchpats.append("Current ping state: (S_IDLE|S_NOT_DC)")
        watchpats.append(self.templates["Pat:NonDC_started"] % node)
        watchpats.append(self.templates["Pat:DC_started"] % node)
        idle_watch = LogWatcher(self.Env["LogFileName"], watchpats, "ClusterIdle", hosts=[node], kind=self.Env["LogWatcher"])
        idle_watch.setwatch()

        out = self.rsh(node, self.templates["StatusCmd"]%node, 1)
        self.debug("Node %s status: '%s'" %(node, out))            

        if not out or (out.find('ok') < 0):
            if self.ShouldBeStatus[node] == "up":
                self.log(
                    "Node status for %s is %s but we think it should be %s"
                    % (node, "down", self.ShouldBeStatus[node]))
            self.ShouldBeStatus[node] = "down"
            return 0

        if self.ShouldBeStatus[node] == "down":
            self.log(
                "Node status for %s is %s but we think it should be %s: %s"
                % (node, "up", self.ShouldBeStatus[node], out))

        self.ShouldBeStatus[node] = "up"

        # check the output first - because syslog-ng loses messages
        if out.find('S_NOT_DC') != -1:
            # Up and stable
            return 2
        if out.find('S_IDLE') != -1:
            # Up and stable
            return 2

        # fall back to syslog-ng and wait
        if not idle_watch.look():
            # just up
            self.debug("Warn: Node %s is unstable: %s" % (node, out))
            return 1

        # Up and stable
        return 2

    # Is the node up or is the node down
    def StataCM(self, node):
        '''Report the status of the cluster manager on a given node'''

        if self.test_node_CM(node) > 0:
            return 1
        return None

    # Being up and being stable is not the same question...
    def node_stable(self, node):
        '''Report the status of the cluster manager on a given node'''

        if self.test_node_CM(node) == 2:
            return 1
        self.log("Warn: Node %s not stable" % (node)) 
        return None

    def partition_stable(self, nodes, timeout=None):
        watchpats = [ ]
        watchpats.append("Current ping state: S_IDLE")
        watchpats.append(self.templates["Pat:DC_IDLE"])
        self.debug("Waiting for cluster stability...") 

        if timeout == None:
            timeout = self.Env["DeadTime"]

        if len(nodes) < 3:
            self.debug("Cluster is inactive") 
            return 1

        idle_watch = LogWatcher(self.Env["LogFileName"], watchpats, "ClusterStable", timeout, hosts=nodes.split(), kind=self.Env["LogWatcher"])
        idle_watch.setwatch()

        for node in nodes.split():
            # have each node dump its current state
            self.rsh(node, self.templates["StatusCmd"] % node, 1)

        ret = idle_watch.look()
        while ret:
            self.debug(ret) 
            for node in nodes.split():
                if re.search(node, ret):
                    return 1
            ret = idle_watch.look()

        self.debug("Warn: Partition %s not IDLE after %ds" % (repr(nodes), timeout)) 
        return None

    def cluster_stable(self, timeout=None, double_check=False):
        partitions = self.find_partitions()

        for partition in partitions:
            if not self.partition_stable(partition, timeout):
                return None

        if double_check:
            # Make sure we are really stable and that all resources,
            # including those that depend on transient node attributes,
            # are started if they were going to be
            time.sleep(5)
            for partition in partitions:
                if not self.partition_stable(partition, timeout):
                    return None

        return 1

    def is_node_dc(self, node, status_line=None):
        rc = 0

        if not status_line: 
            status_line = self.rsh(node, self.templates["StatusCmd"]%node, 1)

        if not status_line:
            rc = 0
        elif status_line.find('S_IDLE') != -1:
            rc = 1
        elif status_line.find('S_INTEGRATION') != -1:
            rc = 1
        elif status_line.find('S_FINALIZE_JOIN') != -1:
            rc = 1
        elif status_line.find('S_POLICY_ENGINE') != -1:
            rc = 1
        elif status_line.find('S_TRANSITION_ENGINE') != -1:
            rc = 1

        return rc

    def active_resources(self, node):
        (rc, output) = self.rsh(node, """crm_resource -c""", None)
        resources = []
        for line in output: 
            if re.search("^Resource", line):
                tmp = AuditResource(self, line)
                if tmp.type == "primitive" and tmp.host == node:
                    resources.append(tmp.id)
        return resources

    def ResourceLocation(self, rid):
        ResourceNodes = []
        for node in self.Env["nodes"]:
            if self.ShouldBeStatus[node] == "up":

                cmd = self.templates["RscRunning"] % (rid)
                (rc, lines) = self.rsh(node, cmd, None)

                if rc == 127:
                    self.log("Command '%s' failed. Binary or pacemaker-cts package not installed?" % cmd)
                    for line in lines:
                        self.log("Output: "+line)
                elif rc == 0:
                    ResourceNodes.append(node)

        return ResourceNodes

    def find_partitions(self):
        ccm_partitions = []

        for node in self.Env["nodes"]:
            if self.ShouldBeStatus[node] == "up":
                partition = self.rsh(node, self.templates["PartitionCmd"], 1)

                if not partition:
                    self.log("no partition details for %s" % node)
                elif len(partition) > 2:
                    nodes = partition.split()
                    nodes.sort()
                    partition = ' '.join(nodes)

                    found = 0
                    for a_partition in ccm_partitions:
                        if partition == a_partition:
                            found = 1
                    if found == 0:
                        self.debug("Adding partition from %s: %s" % (node, partition))
                        ccm_partitions.append(partition)
                    else:
                        self.debug("Partition '%s' from %s is consistent with existing entries" % (partition, node))

                else:
                    self.log("bad partition details for %s" % node)
            else:
                self.debug("Node %s is down... skipping" % node)

        self.debug("Found partitions: %s" % repr(ccm_partitions) )
        return ccm_partitions

    def HasQuorum(self, node_list):
        # If we are auditing a partition, then one side will
        #   have quorum and the other not.
        # So the caller needs to tell us which we are checking
        # If no value for node_list is specified... assume all nodes  
        if not node_list:
            node_list = self.Env["nodes"]

        for node in node_list:
            if self.ShouldBeStatus[node] == "up":
                quorum = self.rsh(node, self.templates["QuorumCmd"], 1)
                if quorum.find("1") != -1:
                    return 1
                elif quorum.find("0") != -1:
                    return 0
                else:
                    self.debug("WARN: Unexpected quorum test result from " + node + ":" + quorum)

        return 0
    def Components(self):    
        complist = []
        common_ignore = [
                    "Pending action:",
                    "(ERROR|error): crm_log_message_adv:",
                    "(ERROR|error): MSG: No message to dump",
                    "pending LRM operations at shutdown",
                    "Lost connection to the CIB manager",
                    "Connection to the CIB terminated...",
                    "Sending message to the CIB manager FAILED",
                    "Action A_RECOVER .* not supported",
                    "(ERROR|error): stonithd_op_result_ready: not signed on",
                    "pingd.*(ERROR|error): send_update: Could not send update",
                    "send_ipc_message: IPC Channel to .* is not connected",
                    "unconfirmed_actions: Waiting on .* unconfirmed actions",
                    "cib_native_msgready: Message pending on command channel",
                    r": Performing A_EXIT_1 - forcefully exiting ",
                    r"Resource .* was active at shutdown.  You may ignore this error if it is unmanaged.",
            ]

        stonith_ignore = [
            r"Updating failcount for child_DoFencing",
            r"error.*: Fencer connection failed \(will retry\)",
            "pacemaker-execd.*(ERROR|error): stonithd_receive_ops_result failed.",
             ]

        stonith_ignore.extend(common_ignore)

        ccm = Process(self, "ccm", triggersreboot=self.fastfail, pats = [
                    "State transition .* S_RECOVERY",
                    "pacemaker-controld.*Action A_RECOVER .* not supported",
                    r"pacemaker-controld.*: Input I_TERMINATE .*from do_recover",
                    r"pacemaker-controld.*: Could not recover from internal error",
                    "pacemaker-controld.*I_ERROR.*crmd_cib_connection_destroy",
                    # these status numbers are likely wrong now
                    r"pacemaker-controld.*exited with status 2",
                    r"attrd.*exited with status 1",
                    r"cib.*exited with status 2",

# Not if it was fenced
#                    "A new node joined the cluster",

#                    "WARN: determine_online_status: Node .* is unclean",
#                    "Scheduling Node .* for STONITH",
#                    "Executing .* fencing operation",
#                    "tengine_stonith_callback: .*result=0",
#                    "Processing I_NODE_JOIN:.* cause=C_HA_MESSAGE",
#                    "State transition S_.* -> S_INTEGRATION.*input=I_NODE_JOIN",
                    "State transition S_STARTING -> S_PENDING",
                    ], badnews_ignore = common_ignore)

        based = Process(self, "pacemaker-based", triggersreboot=self.fastfail, pats = [
                    "State transition .* S_RECOVERY",
                    "Lost connection to the CIB manager",
                    "Connection to the CIB manager terminated",
                    r"pacemaker-controld.*: Input I_TERMINATE .*from do_recover",
                    "pacemaker-controld.*I_ERROR.*crmd_cib_connection_destroy",
                    r"pacemaker-controld.*: Could not recover from internal error",
                    # these status numbers are likely wrong now
                    r"pacemaker-controld.*exited with status 2",
                    r"attrd.*exited with status 1",
                    ], badnews_ignore = common_ignore)

        execd = Process(self, "pacemaker-execd", triggersreboot=self.fastfail, pats = [
                    "State transition .* S_RECOVERY",
                    "LRM Connection failed",
                    "pacemaker-controld.*I_ERROR.*lrm_connection_destroy",
                    "State transition S_STARTING -> S_PENDING",
                    r"pacemaker-controld.*: Input I_TERMINATE .*from do_recover",
                    r"pacemaker-controld.*: Could not recover from internal error",
                    # this status number is likely wrong now
                    r"pacemaker-controld.*exited with status 2",
                    ], badnews_ignore = common_ignore)

        controld = Process(self, "pacemaker-controld", triggersreboot=self.fastfail,
                    pats = [
#                    "WARN: determine_online_status: Node .* is unclean",
#                    "Scheduling Node .* for STONITH",
#                    "Executing .* fencing operation",
#                    "tengine_stonith_callback: .*result=0",
                    "State transition .* S_IDLE",
                    "State transition S_STARTING -> S_PENDING",
                    ], badnews_ignore = common_ignore)

        schedulerd = Process(self, "pacemaker-schedulerd", triggersreboot=self.fastfail, pats = [
                    "State transition .* S_RECOVERY",
                    r"pacemaker-controld.*: Input I_TERMINATE .*from do_recover",
                    r"pacemaker-controld.*: Could not recover from internal error",
                    r"pacemaker-controld.*CRIT.*: Connection to the scheduler failed",
                    "pacemaker-controld.*I_ERROR.*save_cib_contents",
                    # this status number is likely wrong now
                    r"pacemaker-controld.*exited with status 2",
                    ], badnews_ignore = common_ignore, dc_only=1)

        if self.Env["DoFencing"] == 1 :
            complist.append(Process(self, "stoniths", triggersreboot=self.fastfail, dc_pats = [
                        r"pacemaker-controld.*CRIT.*: Fencing daemon connection failed",
                        "Attempting connection to fencing daemon",
                    ], badnews_ignore = stonith_ignore))

        if self.fastfail == 0:
            ccm.pats.extend([
                # these status numbers are likely wrong now
                r"attrd.*exited with status 1",
                r"pacemaker-(based|controld).*exited with status 2",
                ])
            based.pats.extend([
                # these status numbers are likely wrong now
                r"attrd.*exited with status 1",
                r"pacemaker-controld.*exited with status 2",
                ])
            execd.pats.extend([
                # these status numbers are likely wrong now
                r"pacemaker-controld.*exited with status 2",
                ])

        complist.append(ccm)
        complist.append(based)
        complist.append(execd)
        complist.append(controld)
        complist.append(schedulerd)

        return complist

    def StandbyStatus(self, node):
        out=self.rsh(node, self.templates["StandbyQueryCmd"] % node, 1)
        if not out:
            return "off"
        out = out[:-1]
        self.debug("Standby result: "+out)
        return out

    # status == "on" : Enter Standby mode
    # status == "off": Enter Active mode
    def SetStandbyMode(self, node, status):
        current_status = self.StandbyStatus(node)
        cmd = self.templates["StandbyCmd"] % (node, status)
        ret = self.rsh(node, cmd)
        return True

    def AddDummyRsc(self, node, rid):
        rsc_xml = """ '<resources>
                <primitive class=\"ocf\" id=\"%s\" provider=\"pacemaker\" type=\"Dummy\">
                    <operations>
                        <op id=\"%s-interval-10s\" interval=\"10s\" name=\"monitor\"/
                    </operations>
                </primitive>
            </resources>'""" % (rid, rid)
        constraint_xml = """ '<constraints>
                <rsc_location id=\"location-%s-%s\" node=\"%s\" rsc=\"%s\" score=\"INFINITY\"/>
            </constraints>'
            """ % (rid, node, node, rid)

        self.rsh(node, self.templates['CibAddXml'] % (rsc_xml))
        self.rsh(node, self.templates['CibAddXml'] % (constraint_xml))

    def RemoveDummyRsc(self, node, rid):
        constraint = "\"//rsc_location[@rsc='%s']\"" % (rid)
        rsc = "\"//primitive[@id='%s']\"" % (rid)

        self.rsh(node, self.templates['CibDelXpath'] % constraint)
        self.rsh(node, self.templates['CibDelXpath'] % rsc)


#######################################################################
#
#   A little test code...
#
#   Which you are advised to completely ignore...
#
#######################################################################
if __name__ == '__main__': 
    pass
