#!/usr/bin/env python3
"""FreeBSD ELF-to-Image boot ABI layout and refusal controls."""
import struct
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from compose_pcie2_contract_dtb import decode, encode
import unittest
from unittest.mock import patch
import run_pi5_freebsd
from prepare_pi5_boot import kernel_image, board_revision
BASE=0xffff000000000000

def fixture():
    b=bytearray(0x108);b[:16]=b'\x7fELF\x02\x01\x01\x09'+bytes(8)
    struct.pack_into('<HHIQQQIHHHHHH',b,16,2,183,1,BASE+0x800,64,0,0,64,56,2,0,0,0)
    struct.pack_into('<IIQQQQQQ',b,64,1,4,0,BASE,BASE,64,64,0x10000)
    struct.pack_into('<IIQQQQQQ',b,120,1,5,0x100,BASE+0x800,BASE+0x800,8,0x1000,0x10000)
    b[0x100:]=bytes.fromhex('1f2003d5c0035fd6') # independent AArch64 NOP; RET
    return b

class Conversion(unittest.TestCase):
    def test_image_header_and_exact_code(self):
        image=kernel_image(fixture())
        self.assertEqual(struct.unpack_from('<I',image)[0],0x14000200)
        self.assertEqual(struct.unpack_from('<Q',image,8)[0],0x200000)
        self.assertEqual(image[0x38:0x3c],b'ARM\x64')
        self.assertEqual(image[0x800:0x808],bytes.fromhex('1f2003d5c0035fd6'))
        self.assertEqual(image[0x808:0x1800],bytes(0xff8))
    def test_prepared_fdt_capacity_and_firmware_hole(self):
        soc='/soc@107c000000'
        paths=['/chosen','/memory@0',soc,soc+'/mmc@fff000',
               soc+'/serial@7d001000',soc+'/interrupt-controller@7fff9000',
               soc+'/gpio@7d517c00',soc+'/rng@7d208000',
               soc+'/pinctrl@7d504100',soc+'/pinctrl@7d510700',
               '/timer','/psci','/sd-io-1v8-reg','/sd-vcc-reg']
        nodes={path:{} for path in paths}
        nodes['/']={'compatible':b'raspberrypi,5-model-b\0brcm,bcm2712\0',
                    '#address-cells':struct.pack('>I',2),
                    '#size-cells':struct.pack('>I',1)}
        nodes['/memory@0']={'device_type':b'memory\0'}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'kernel').write_bytes(fixture())
            for size_cells in (1,2):
                nodes['/']['#size-cells']=struct.pack('>I',size_cells)
                (root/'input.dtb').write_bytes(encode(nodes,bytes(16),0))
                for size,count in ((1,1),(2,2),(4,3),(8,5),(16,9)):
                    with self.subTest(memory_gib=size):
                        output=root/f'{size_cells}-{size}'
                        result=subprocess.run([sys.executable,
                            str(Path(__file__).with_name('prepare_pi5_boot.py')),
                            '--dtb',str(root/'input.dtb'),'--kernel',str(root/'kernel'),
                            '--output',str(output),'--memory-gib',str(size)],
                            capture_output=True,text=True,timeout=10)
                        self.assertEqual(result.returncode,0,result.stderr)
                        derived,_,_=decode((output/'pi5-sd.dtb').read_bytes())
                        self.assertEqual(derived['/']['#size-cells'],struct.pack('>I',size_cells))
                        reg=derived['/memory@0']['reg'];width=(2+size_cells)*4
                        self.assertEqual(len(reg)%width,0)
                        banks=[(int.from_bytes(reg[i:i+8],'big'),int.from_bytes(reg[i+8:i+width],'big'))
                               for i in range(0,len(reg),width)]
                        self.assertEqual(len(banks),count)
                        self.assertEqual(banks[0],(0,0x3c000000))
                        self.assertEqual(sum(n for _,n in banks),size*(1<<30)-(64<<20))
                        manifest=json.loads((output/'inputs.json').read_text())
                        self.assertEqual(manifest['board_revision'],board_revision(derived,size))
                        self.assertTrue(all(a+n<=size*(1<<30) for a,n in banks))
                        self.assertTrue(all(a+n<=b for (a,n),(b,_) in zip(banks,banks[1:])))
    def test_board_identity_profiles(self):
        # Independent documented board-type and memory fields, including the
        # retained Model B revision for its 1/16 GiB profiles.
        for family, board, sizes in ((b'raspberrypi,5-model-b',0x17,(1,2,4,8,16)),
                                     (b'raspberrypi,500',0x19,(8,16))):
            nodes={'/':{'compatible':family+b'\0brcm,bcm2712\0'}}
            for size in sizes:
                with self.subTest(family=family,memory_gib=size):
                    value=board_revision(nodes,size)
                    self.assertEqual((value>>4)&0xff,board)
                    self.assertEqual((value>>20)&7,{1:2,2:3,4:4,8:5,16:6}[size])
                    self.assertEqual(value&15,int(board==0x17 and size in (1,16)))
        for size in (1,2,4):
            with self.assertRaisesRegex(ValueError,'Pi500'):
                board_revision({'/':{'compatible':b'raspberrypi,500\0brcm,bcm2712\0'}},size)
    def test_unenrolled_and_ambiguous_board_refused(self):
        for compatible in (b'brcm,bcm2712\0',
                           b'raspberrypi,500\0brcm,bcm2711\0',
                           b'raspberrypi,500\0raspberrypi,5-model-b\0brcm,bcm2712\0'):
            with self.subTest(compatible=compatible),self.assertRaises(ValueError):
                board_revision({'/':{'compatible':compatible}},8)
    def direct_inputs(self, root, revision):
        nodes={'/':{'compatible':b'raspberrypi,500\0brcm,bcm2712\0'}}
        for name in ('qemu-system-aarch64','qemu-img','FreeBSD-Image','disk.img'):
            (root/name).write_bytes(b'independent control input')
        (root/'pi5-sd.dtb').write_bytes(encode(nodes,bytes(16),0))
        manifest={'memory_gib':8,'image_sha256':run_pi5_freebsd.sha(root/'FreeBSD-Image'),
                  'derived_dtb_sha256':run_pi5_freebsd.sha(root/'pi5-sd.dtb')}
        if revision is not None:manifest['board_revision']=revision
        (root/'inputs.json').write_text(json.dumps(manifest))
        return ['run_pi5_freebsd.py','--qemu',str(root/'qemu-system-aarch64'),
                '--boot-inputs',str(root),'--disk',str(root/'disk.img'),
                '--output',str(root/'result')]
    def test_direct_runner_refuses_old_or_cross_board_manifest(self):
        for revision in (None,0xd04170):
            with self.subTest(revision=revision),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);argv=self.direct_inputs(root,revision)
                with patch.object(sys,'argv',argv),patch.object(run_pi5_freebsd.subprocess,'run') as command:
                    with self.assertRaisesRegex(ValueError,'board identity mismatch'):
                        run_pi5_freebsd.main()
                    command.assert_not_called()
                    self.assertFalse((root/'result').exists())
    def test_direct_runner_selects_enrolled_pi500(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);argv=self.direct_inputs(root,0xd04190)
            with patch.object(sys,'argv',argv),patch.object(run_pi5_freebsd.subprocess,'run'), \
                 patch.object(run_pi5_freebsd.subprocess,'Popen',side_effect=OSError('captured launch')) as launch:
                with self.assertRaisesRegex(OSError,'captured launch'):
                    run_pi5_freebsd.main()
                command=launch.call_args.args[0]
                self.assertEqual(command[command.index('-M')+1],'raspi5b,board-revision=0xd04190')
    def test_os_memory_accounts_for_all_five_profiles(self):
        for size in (1,2,4,8,16):
            usable=size*(1<<30)-(64<<20)
            for page in (4096,65536):
                with self.subTest(memory_gib=size,page_size=page):
                    serial=f'real memory  = {usable-page} (guest memory)\n'
                    stats=f'hw.physmem: {usable-(24<<20)}\nhw.pagesize: {page}\n'
                    self.assertTrue(all(run_pi5_freebsd.memory_checks(serial,stats,size).values()))
    def test_os_memory_rejects_missing_bank_hidden_by_percentage(self):
        usable=16*(1<<30)-(64<<20)
        serial=f'real memory  = {usable-(1<<30)-4096}\n'
        stats=f'hw.physmem: {usable-(1<<30)-(24<<20)}\nhw.pagesize: 4096\n'
        checks=run_pi5_freebsd.memory_checks(serial,stats,16)
        self.assertTrue(checks['full_modeled_memory'])
        self.assertFalse(checks['memory_banks_exact'])
    def test_os_memory_requires_page_size_and_correct_page_zero(self):
        usable=(1<<30)-(64<<20)
        for serial,stats in ((f'real memory = {usable-8192}',f'hw.physmem: {usable-(24<<20)}\nhw.pagesize: 4096\n'),
                             (f'real memory = {usable-4096}',f'hw.physmem: {usable-(24<<20)}\n'),
                             (f'real memory = {usable-3}',f'hw.physmem: {usable-(24<<20)}\nhw.pagesize: 3\n')):
            with self.subTest(serial=serial,stats=stats):
                self.assertFalse(run_pi5_freebsd.memory_checks(serial,stats,1)['memory_banks_exact'])
    def test_short_header(self):
        with self.assertRaises(ValueError):kernel_image(b'\x7fELF')
    def test_non_arm_kernel(self):
        b=fixture();struct.pack_into('<H',b,18,62)
        with self.assertRaises(ValueError):kernel_image(b)
    def test_program_table_bounds(self):
        b=fixture();struct.pack_into('<H',b,56,255)
        with self.assertRaises(ValueError):kernel_image(b)
    def test_load_segment_outside_file(self):
        b=fixture();struct.pack_into('<Q',b,120+32,0x1000)
        with self.assertRaises(ValueError):kernel_image(b)
    def test_overlapping_segments(self):
        b=fixture();struct.pack_into('<QQ',b,120+16,BASE+32,BASE+32)
        with self.assertRaises(ValueError):kernel_image(b)
    def test_wrong_virtual_base(self):
        b=fixture();struct.pack_into('<QQ',b,64+16,0,0)
        with self.assertRaises(ValueError):kernel_image(b)
    def test_entry_in_header(self):
        b=fixture();struct.pack_into('<I',b,64+4,5)
        with self.assertRaises(ValueError):kernel_image(b)

if __name__=='__main__':unittest.main()
