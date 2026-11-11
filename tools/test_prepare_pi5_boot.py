#!/usr/bin/env python3
"""FreeBSD ELF-to-Image boot ABI layout and refusal controls."""
import struct
from pathlib import Path
import subprocess
import sys
import tempfile
from compose_pcie2_contract_dtb import decode, encode
import unittest
from prepare_pi5_boot import kernel_image
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
        nodes['/']={'compatible':b'brcm,bcm2712\0',
                    '#address-cells':struct.pack('>I',2),
                    '#size-cells':struct.pack('>I',1)}
        nodes['/memory@0']={'device_type':b'memory\0'}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'kernel').write_bytes(fixture())
            (root/'input.dtb').write_bytes(encode(nodes,bytes(16),0))
            for size,count in ((1,1),(2,2),(4,3),(8,5),(16,9)):
                with self.subTest(memory_gib=size):
                    output=root/str(size)
                    result=subprocess.run([sys.executable,
                        str(Path(__file__).with_name('prepare_pi5_boot.py')),
                        '--dtb',str(root/'input.dtb'),'--kernel',str(root/'kernel'),
                        '--output',str(output),'--memory-gib',str(size)],
                        capture_output=True,text=True,timeout=10)
                    self.assertEqual(result.returncode,0,result.stderr)
                    derived,_,_=decode((output/'pi5-sd.dtb').read_bytes())
                    banks=[(hi<<32|lo,n) for hi,lo,n in
                           struct.iter_unpack('>III',derived['/memory@0']['reg'])]
                    self.assertEqual(len(banks),count)
                    self.assertEqual(banks[0],(0,0x3c000000))
                    self.assertEqual(sum(n for _,n in banks),size*(1<<30)-(64<<20))
                    self.assertTrue(all(a+n<=size*(1<<30) for a,n in banks))
                    self.assertTrue(all(a+n<=b for (a,n),(b,_) in zip(banks,banks[1:])))
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
