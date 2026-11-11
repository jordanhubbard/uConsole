#!/usr/bin/env python3
"""Pi5 boot-device digital MMIO, actual card detect, migration and reset guards."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import struct
import tempfile
from test_emulator_pcie_xhci import Machine
from test_emulator_pmic import connect
from test_emulator_adc_migration import migrate
from uconsole_emulator import qmp

CFG=0x1000fff400
PIN=0x107d504100
AON=0x107d510700
GPIO=0x107d517c00
RNG=0x107d208000
MBOX=0x107c013880
MIP=0x1000130000
GICD=0x107fff9000
SDIO=0x1001100000

class Pi5Machine(Machine):
    def __init__(self,root,name,stack,binary,card=None,incoming=False,memory_gib=2,revision=None,boot_args=()):
        self.monitor=root/(name+'-qmp');test=root/(name+'-qtest')
        log=stack.enter_context((root/(name+'.log')).open('wb'))
        board='raspi5b'+('' if revision is None else f',board-revision={revision:#x}')
        cmd=[binary,'-M',board,'-m',f'{memory_gib}G','-accel','qtest','-S','-display','none','-serial','none','-monitor','none','-nic','none',
             '-qtest',f'unix:{test},server=on,wait=off','-qmp',f'unix:{self.monitor},server=on,wait=off']
        cmd+=list(boot_args)
        if card:cmd+=['-drive',f'if=sd,format=raw,file={card}']
        if incoming:cmd+=['-incoming','defer']
        self.process=subprocess.Popen(cmd,stdout=log,stderr=log);stack.callback(self.close)
        try:self.sock=stack.enter_context(connect(test,self.process))
        except Exception:
            print(Path(log.name).read_text(errors='replace'));raise
        self.stream=stack.enter_context(self.sock.makefile('rwb',buffering=0))

def run(binary):
    with tempfile.TemporaryDirectory(prefix='p5-',dir='/tmp') as temp,ExitStack() as stack:
        root=Path(temp);m=Pi5Machine(root,'source',stack,binary)
        assert m.read(CFG)==0 and m.read(CFG+0x1ac)==0
        m.write(CFG,0xffffffff);assert m.read(CFG)==0xc0000000
        m.write(CFG+0x1ac,0xffffffff);assert m.read(CFG+0x1ac)==0x80000001
        m.write(CFG+4,0x12345678);assert m.read(CFG+4)==0
        assert m.read(GPIO+8)==0x7fff and m.read(GPIO+40)==0x3f
        assert m.read(GPIO+4)&32  # absent real SD backend => CD_N high
        m.write(GPIO+4,0xffffffff);assert m.read(GPIO+4)==0x7fff
        m.write(GPIO+36,0xffffffff);assert m.read(GPIO+36)==0x3f
        m.write(GPIO+0x1c,0xffffffff);assert m.read(GPIO+0x1c)==0
        m.write(PIN+0x18,0x000aa080);m.write(AON+0xc,0x00400000)
        assert m.read(PIN+0x18)==0x000aa080 and m.read(AON+0xc)==0x00400000
        cpus=qmp(m.monitor,'query-cpus-fast')
        assert len(cpus)==4
        for i,cpu in enumerate(cpus):
            v=qmp(m.monitor,'qom-get',{'path':cpu['qom-path'],'property':'mp-affinity'})
            assert v==i<<8
        assert m.read(RNG+0x24)&0xff==0
        m.write(RNG,1);assert m.read(RNG+0x24)&0xff
        assert len({m.read(RNG+0x20) for _ in range(8)})>1
        m.write(RNG,0)
        # Real VideoCore property request through the BCM2712 mailbox window.
        # The GPU bus alias translates DMA only; CPU RAM at that address stays
        # distinct. Leave the response pending across migration.
        assert m.read(MBOX+0x18) & 0x40000000
        request=struct.pack('<8I',32,0,0x10005,8,0,0,0,0)
        m.command(f'write 0x100000 0x20 0x{request.hex()}')
        m.write(0x40100000,0x55aa55aa)
        m.write(MBOX+0x20,0x40100008)
        assert not m.read(MBOX+0x18) & 0x40000000
        assert m.read(0x100004)==0x80000000
        assert m.read(0x100014)==0 and m.read(0x100018)==0x3c000000
        assert m.read(0x40100000)==0x55aa55aa
        # A real MIP host-edge message reaches the declared GIC SPI, and a
        # masked message must not latch a pending edge. No PCI/DMA is implied.
        assert m.read(MIP+0x20)==0 and m.read(MIP+0x40)==0xffffffff
        assert not m.read(SDIO+0x24)&0x10000  # no invented SDIO Wi-Fi card
        m.write(MIP+0x20,8)
        m.write(MIP+0x40,0xfffffff7)
        m.write(GICD,1)
        m.write(GICD+0xc28,0x80)  # INTID163 edge, SPI131
        m.write(GICD+0x8a3,1,width='b')  # route INTID163 to CPU0
        m.write(GICD+0x114,8)
        m.write(MIP,3)
        assert m.read(GICD+0x214)&8
        m.write(GICD+0x294,8)
        m.write(MIP+0x40,0xffffffff)
        m.write(MIP,3)
        assert not m.read(GICD+0x214)&8
        m.write(MIP+0x40,0xfffffff7)
        m.write(MIP,3)
        target=Pi5Machine(root,'target',stack,binary,incoming=True)
        migrate(m,target,root)
        assert target.read(MIP+0x20)==8
        assert target.read(MIP+0x40)==0xfffffff7
        assert target.read(GICD+0x214)&8
        assert target.read(CFG)==0xc0000000
        assert target.read(CFG+0x1ac)==0x80000001
        assert target.read(PIN+0x18)==0x000aa080
        assert target.read(AON+0xc)==0x00400000
        assert target.read(GPIO+4)==0x7fff
        assert not target.read(MBOX+0x18) & 0x40000000
        assert target.read(MBOX)==0x40100008
        assert target.read(MBOX+0x18) & 0x40000000
        assert target.read(0x100004)==0x80000000
        assert target.read(0x40100000)==0x55aa55aa
        qmp(target.monitor,'system_reset')
        assert target.read(MBOX+0x18) & 0x40000000
        assert target.read(MIP+0x20)==0 and target.read(MIP+0x40)==0xffffffff
        assert target.read(CFG)==0 and target.read(CFG+0x1ac)==0
        assert target.read(PIN+0x18)==0 and target.read(AON+0xc)==0
        assert target.read(GPIO+8)==0x7fff and target.read(GPIO+4)==32
        card=root/'card.raw'
        with card.open('wb') as f:f.truncate(64*1024*1024)
        present=Pi5Machine(root,'present',stack,binary,card=card)
        assert present.read(GPIO+4)&32==0  # same model, actual card attached
        for bad in [['-m','3G'],['-m','2G','-cpu','cortex-a72']]:
            proc=subprocess.run([binary,'-M','raspi5b',*bad,'-display','none','-serial','none','-monitor','none','-nic','none'],capture_output=True,timeout=10)
            assert proc.returncode!=0 and (b'supports 1, 2, 4, 8 or 16' in proc.stderr or b'CPU type' in proc.stderr or b'CPU model' in proc.stderr),proc.stderr
        for revision in ('0x304170', '0xd04190'):
            # Missing new-scheme bit and mismatched 8 GiB identity on a 2 GiB VM.
            proc=subprocess.run([binary,'-M','raspi5b,board-revision='+revision,
                                 '-m','2G','-display','none','-serial','none',
                                 '-monitor','none','-nic','none'],capture_output=True,timeout=10)
            assert proc.returncode!=0 and b'board-revision' in proc.stderr,proc.stderr
    print('PASS Pi5 MMIO/mailbox DMA aliases/card/CPU topology/migration/reset/refusal controls')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--qemu',required=True);args=p.parse_args();run(str(Path(args.qemu).resolve()))
