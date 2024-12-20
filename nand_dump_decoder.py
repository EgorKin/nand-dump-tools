#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
  NAND Dump Decoder

  Simple software tool for decoding raw dumps of NAND memory chips using
  implemented error correcting codes (ECC) like BCH or Hamming codes
  by Matthias Deeg <matthias.deeg@syss.de>

  based on PMECC reader and decoder by Mickaël Walter
  https://www.mickaelwalter.fr/2018/06/08/dumping-a-slc-nand-flash-with-atmel-pmecc/

  uses BCH library for Python (python-bchlib) by Jeff Kent
  https://github.com/jkent/python-bchlib

  MIT License

  Copyright (c) 2018-2020 SySS GmbH

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in all
  copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  SOFTWARE.
"""

__version__ = '0.3.0'
__author__ = 'Matthias Deeg, Moritz Lottermann, Dmitry Egorkin'

import argparse
import configparser
import bchlib
import mmap
import os
import struct
import sys

from yaffs_ecc import yaffs_extract_ecc, yaffs_calc_ecc_256, yaffs_ecc_correct

#  BCH polynom
ECC_POLY1 = 0x201b       # 8219
ECC_POLY2 = 0x4443       # 17475

# binary dump file extension
DUMP_FILE_EXTENSION = ".bin"


def read_bits(value, low, high, bits=8):
    format_str = "0{:d}b".format(bits)

    n = format(value, format_str)[::-1]
    return int(n[low:high][::-1], 2)


def reverse_bits(data):
    reversed_value = b''
    for i in range(0, len(data)):
        reversed_value += bytes([int('{:08b}'.format(data[i])[::-1], 2)])
    return reversed_value


def xor_crypto(data, key):
    return bytes(a ^ b for a, b in zip(data, key))


def read_atmel_config(page, config):
    """Read ATMEL PMECC configuration from first page"""

    # read ATMEL PMECC header (52 times at the beginning of the first page)
    header = struct.unpack("<L", page[:4])[0]

    # header key
    key = read_bits(header, 28, 32, 32)

    if key != 0x0C:
        print("[-] Error: ATMEL configuration header incorrect")
        sys.exit(1)

    # PMECC usage flag
    config['useecc'] = (read_bits(header, 0, 1, 32) == 1)

    # sector per page
    v = read_bits(header, 1, 4, 31)

    # spare area size
    config['spareareasize'] = read_bits(header, 4, 13, 32)

    # eccBitReq
    v = read_bits(header, 13, 16, 32)

    if v == 0:
        config['ecc_errors'] = 2
    elif v == 1:
        config['ecc_errors'] = 4
    elif v == 2:
        config['ecc_errors'] = 8
    elif v == 3:
        config['ecc_errors'] = 12
    elif v == 4:
        config['ecc_errors'] = 24

    # sector size
    v = read_bits(header, 16, 18, 32)

    if v == 0:
        config['sectorsize'] = 512

        # set required ECC bytes per sector
        if config['ecc_errors'] == 2:
            config['ecc_bytes_per_sector'] = 4
        elif config['ecc_errors'] == 4:
            config['ecc_bytes_per_sector'] = 7
        elif config['ecc_errors'] == 8:
            config['ecc_bytes_per_sector'] = 13
        elif config['ecc_errors'] == 12:
            config['ecc_bytes_per_sector'] = 20
        elif config['ecc_errors'] == 24:
            config['ecc_bytes_per_sector'] = 39

    elif v == 1:
        config['sectorsize'] = 1024

        # set required ECC bytes per sector
        if config['ecc_errors'] == 2:
            config['ecc_bytes_per_sector'] = 4
        elif config['ecc_errors'] == 4:
            config['ecc_bytes_per_sector'] = 7
        elif config['ecc_errors'] == 8:
            config['ecc_bytes_per_sector'] = 14
        elif config['ecc_errors'] == 12:
            config['ecc_bytes_per_sector'] = 21
        elif config['ecc_errors'] == 24:
            config['ecc_bytes_per_sector'] = 42

    # ECC offset in spare area
    config['ecc_offset'] = read_bits(header, 18, 27, 32)

    return config


#my additions
def winbond_error_correction(infiles, outfile, config):
    global start_data, sector_data, sector_ecc
    """Do some error correction"""

    # ECC1 for blank sector (FF only)
    # nullbyte_ecc1 = [0x0A, 0x3A, 0xE9, 0x39, 0x43, 0xDE, 0x09, 0xAC, 0x83, 0x22, 0xD0, 0xE1, 0x7F, 0xF3]

    # ECC2 for blank sector (FF only)
    # nullbyte_ecc2 = [0x79, 0xE8, 0x94, 0x34, 0xA2, 0xD6, 0xB8, 0x41, 0x11, 0x95, 0x93, 0x4B, 0x6B, 0x5B]

    # initialize BCH decoder
    bch = bchlib.BCH(config['ecc_errors'], config['ecc_polynom'])

    # open output file
    fout = open(outfile, "wb")

    # adjust data size depending on configured file offset
    data_size = config['filesize'] - config['file_offset']

    # initialize some variables
    processed_sector_count = 0
    corrected_sector_count = 0
    uncorrected_sector_count = 0
    good_sector_count = 0
    bad_sector_count = 0
    blank_page_count = 0
    bad_block_count = 0
    sectors_per_page = config['sectors_per_page'] # по 2 сектора на странице у нас в дампе
    total_page_count = data_size // config['fullpagesize']
    total_sectors = total_page_count * sectors_per_page
    page_size = config['pagesize']
    block_size_bytes = config['fullpagesize'] * config['blocksize'] # размер одного блока памяти (2 112 байт * 64 = 135 168 байт)
    total_blocks = data_size // block_size_bytes
    
    #config['blocksize'] - балбесы! это оказывается = 64 - кол-во страниц в блоке

    block_offset = config['file_offset'] // block_size_bytes

    # blank page data
    blank_page = b'\xff' * config['fullpagesize'] # полностью пустая страница вся в FF т.к. у нее нет OOB данных

    # work with input files
    input_file_handles = []
    input_file_index = 0
    for f in infiles:
        input_file_handles.append(open(f, "r+b"))

    # memory-map the input files
    input_file_mmaps = []
    for fin in input_file_handles:
        input_file_mmaps.append(mmap.mmap(fin.fileno(), 0))

    # set current input file memory-map
    mm = input_file_mmaps[input_file_index]
    
    
    # ====== search for XOR key used for "encrypting" ECCs in the spare area
    '''Вычисленное ECC для страницы полностью из 00 будет тоже = 00 00 00 00...
       Вычисленное ECC для страницы полностью из FF будет неким числом но в дампах везде FF (так как полностью стертая страница - это FF) и чтобы их движок проверки ECC не метил как с кучей ошибок то делается следующее:
       Для FF-страницы вычисляется ECC, оно XOR с 0xFF и это будет - базовое число.
       И затем для каждой страницы вычисленное ECC перед записью в OOB будет XOR с базовой.
       Т.о. для FF страницы будет ECC XOR базовое = FF FF FF...
       А у страницы из 00 00 записаный в OOB ECC будет как раз базовым числом (т.к. 00 00... XOR базовое = базовое).'''
    nullbyte_sector = b'\x00' * config['sectorsize']
    print("[*] Search for ECC crypto key ...")
    key_found = False
    
    for block in range(total_blocks): # берем каждый блок - от 0 да 1023 включительно
        # read current block data
        start_block = (block + block_offset) * block_size_bytes
        end_block = start_block + block_size_bytes
        block_data = mm[start_block:end_block]

        # check if page has Bad Block marker != FF FF
        b1 = block_data[0x800:0x802]
        if b1 != b'\xff\xff':
            continue

        # process pages in block
        for page in range(config['blocksize']): # берем каждую страницу взятого блока - от 0 да 63 включительно
            start_page = page * config['fullpagesize']
            end_page = start_page + config['fullpagesize']
            page_data = block_data[start_page:end_page]

            # check if page is blank (FF FF FF ...)
            if page_data == blank_page:
                continue
            
            for sector in range(sectors_per_page):
                # use all input files, if required (early exit condition)
                for mmin in input_file_mmaps:
                    # read page of current memory-map
                    start_page = start_block + page * config['fullpagesize']
                    end_page = start_page + config['fullpagesize']
                    page_data = mmin[start_page:end_page]

                    start_data = 0
                    if config['sectors_per_page'] == 4:
                        if sector == 0:
                            sector_data = reverse_bits(page_data[0:0x200])
                            sector_ecc = (page_data[0x410:0x410 + bch.ecc_bytes])
                        if sector == 1:
                            sector_data = reverse_bits(page_data[0x200:0x400])
                            sector_ecc = (page_data[0x417:0x417 + bch.ecc_bytes])
                        if sector == 2:
                            sector_data = reverse_bits(page_data[0x400:0x410] + page_data[0x41E:0x60E])
                            sector_ecc = (page_data[0x810:0x810 + bch.ecc_bytes])
                        if sector == 3:
                            sector_data = reverse_bits(page_data[0x60E:0x800] + page_data[0x802:0x810])
                            sector_ecc = (page_data[0x817:0x817 + bch.ecc_bytes])
                    else:
                        if config['sectors_per_page'] == 2:
                            if sector == 0:
                                sector_data = reverse_bits(page_data[0:0x410])
                                sector_ecc = (page_data[0x410:0x410 + bch.ecc_bytes])
                            if sector == 1:
                                sector_data = reverse_bits(page_data[0x41E:0x800] + page_data[0x802:0x810] + page_data[0x800:0x802] + page_data[0x81E:0x83C])
                                sector_ecc = (page_data[0x810:0x810 + bch.ecc_bytes])
                        else:
                            if sector == 0:
                                # get data of current sector
                                start_data = 0
                                end_data = start_data + config['sectorsize'] + config['metadata_size']
                                sector_data = reverse_bits(page_data[start_data:end_data])
                                # get ECC of current sector
                                start_ecc = config['sectorsize'] + config['metadata_size'] # 512 + 0
                                end_ecc = start_ecc + int(config['ecc_bytes_per_sector'])
                                sector_ecc = (page_data[start_ecc:end_ecc])
                            else:
                                # get data of current sector
                                start_data = sector * config['sectorsize'] + config['metadata_size'] + sector * int(config['ecc_bytes_per_sector'])
                                end_data = start_data + config['sectorsize']
                                sector_data = reverse_bits(page_data[start_data:end_data])
                                # get ECC of current sector
                                start_ecc = start_data + config['sectorsize']
                                end_ecc = start_ecc + int(config['ecc_bytes_per_sector'])
                                sector_ecc = (page_data[start_ecc:end_ecc])
                        
                    if sector_data == nullbyte_sector:
                        nullbyte_ecc = sector_ecc
                        # we have found our xor key, so stop searching
                        # should be same as nullbyte_ecc1 for W25N01GV flash chip (it was hardcoded early)
                        print("[*] Found at 0x%08X ECC crypto key: {}".format(nullbyte_ecc.hex()) % (start_block + start_page + start_data))
                        key_found = True
                        break
                # if the XOR key was found, stop searching
                if key_found:
                    break
            # if the XOR key was found, stop searching
            if key_found:
                break
        # if the XOR key was found, stop searching
            if key_found:
                break
    
    if not key_found:
        print("[-] Could not find the ECC crypto key. Please use other dump files or fix config")
        sys.exit(1)
    # ====== 

    # with open(infile, "rb") as fin:
    print("[*] Starting error correcting process ...")

    for block in range(total_blocks): # берем каждый блок - от 0 да 1023 включительно
        # read current block data
        start_block = (block + block_offset) * block_size_bytes
        end_block = start_block + block_size_bytes
        block_data = mm[start_block:end_block]

        # check if page has Bad Block marker != FF FF
        b1 = block_data[0x800:0x802]
        if b1 != b'\xff\xff':
            # increment bad block counter
            bad_block_count += 1
            continue

        # process pages in block
        for page in range(config['blocksize']): # берем каждую страницу взятого блока - от 0 да 63 включительно
            start_page = page * config['fullpagesize']
            end_page = start_page + config['fullpagesize']
            page_data = block_data[start_page:end_page]

            # check if page is blank (FF FF FF ...)
            if page_data == blank_page:
                # increment blank page counter
                blank_page_count += 1

                # increment good sector counter
                good_sector_count += sectors_per_page

                # increment count of processed sectors
                processed_sector_count += sectors_per_page

                # write blank page to output file
                fout.write(blank_page[:page_size]) # запишем 2048 байт 0xFF раз у нас страница пустая

                # show some statistics during processing all sectors
                ##progress = processed_sector_count / total_sectors * 100
                ##print("\r    Progress: {:.2f}% ({}/{} sectors)".format(progress, processed_sector_count, total_sectors), end="")
                if (page+1)%64 == 0:
                    print("\033[90m█\033[0m") # blank page symbol
                else:
                    print("\033[90m█\033[0m", end='')
                continue

            # if page not blank start processing sectors in page
            my_page_mark = 0 # no errors at this sector
            
            # process remaining sectors
            for sector in range(sectors_per_page):
                # initialize bad sector flag
                bad_sector = False

                # increment count of processed sectors
                processed_sector_count += 1

                # use all input files, if required (early exit condition)
                for mmin in input_file_mmaps:
                    # read page of current memory-map
                    start_page = start_block + page * config['fullpagesize']
                    end_page = start_page + config['fullpagesize']
                    page_data = mmin[start_page:end_page]

                    if config['sectors_per_page'] == 4:
                        if sector == 0:
                            sector_data = reverse_bits(page_data[0:0x200])
                            sector_ecc = (page_data[0x410:0x410 + bch.ecc_bytes])
                        if sector == 1:
                            sector_data = reverse_bits(page_data[0x200:0x400])
                            sector_ecc = (page_data[0x417:0x417 + bch.ecc_bytes])
                        if sector == 2:
                            sector_data = reverse_bits(page_data[0x400:0x410] + page_data[0x41E:0x60E])
                            sector_ecc = (page_data[0x810:0x810 + bch.ecc_bytes])
                        if sector == 3:
                            sector_data = reverse_bits(page_data[0x60E:0x800] + page_data[0x802:0x810])
                            sector_ecc = (page_data[0x817:0x817 + bch.ecc_bytes])
                    else:
                        if config['sectors_per_page'] == 2:
                            if sector == 0:
                                sector_data = reverse_bits(page_data[0:0x410])
                                sector_ecc = (page_data[0x410:0x410 + bch.ecc_bytes])
                            if sector == 1:
                                sector_data = reverse_bits(page_data[0x41E:0x800] + page_data[0x802:0x810] + page_data[0x800:0x802] + page_data[0x81E:0x83C]) # у второго серктора ЕСС считается также по 1040 байтам хотя нормальных данных тут только 1008, остальное OOB
                                sector_ecc = (page_data[0x810:0x810 + bch.ecc_bytes])
                        else:
                            if sector == 0:
                                # get data of current sector
                                start_data = 0
                                end_data = start_data + config['sectorsize'] + config['metadata_size']
                                sector_data = reverse_bits(page_data[start_data:end_data])
                                # get ECC of current sector
                                start_ecc = config['sectorsize'] + config['metadata_size'] # 512 + 0
                                end_ecc = start_ecc + int(config['ecc_bytes_per_sector'])
                                sector_ecc = (page_data[start_ecc:end_ecc])
                            else:
                                # get data of current sector
                                start_data = sector * config['sectorsize'] + config['metadata_size'] + sector * int(config['ecc_bytes_per_sector'])
                                end_data = start_data + config['sectorsize']
                                sector_data = reverse_bits(page_data[start_data:end_data])
                                # get ECC of current sector
                                start_ecc = start_data + config['sectorsize']
                                end_ecc = start_ecc + int(config['ecc_bytes_per_sector'])
                                sector_ecc = (page_data[start_ecc:end_ecc])

                    # # calculate ECC
                    # calc_ecc = bch.encode(sector_data)

                    # check that data is correct
                    if len(sector_data)*8 > (bch.n - bch.ecc_bits):
                        print("data len = %i should be <= ecc (n - bits) = %i - %i" % (len(sector_data)*8, bch.n, bch.ecc_bits))
                    if len(sector_ecc) != bch.ecc_bytes:
                        print("Error: file read ECC len=%i, conf ECC len=%i" % (len(sector_ecc), bch.ecc_bytes))
                    
                    # decrypt sector ECC with xor key
                    decrypted_ecc = xor_crypto(sector_ecc, nullbyte_ecc)
            
                    # apply BCH error correcting code
                    corrected = bch.decode(sector_data, reverse_bits(decrypted_ecc))

                    corrected_data = bytearray(sector_data)
                    corrected_ecc = bytearray(sector_ecc)
                    bch.correct(corrected_data, corrected_ecc)

                    # set expected size for sector
                    if sector == 0:
                        expected_size = config['sectorsize'] + config['metadata_size']
                    else:
                        expected_size = config['sectorsize']

                    if corrected >= 0 and corrected <= bch.t:
                        # correctable number of errors

                        if len(corrected_data) == expected_size:
                            # write corrected sector data to output file
                            if config['sectors_per_page'] == 2:
                                # skip metadata for first sector in page
                                if sector == 0:
                                    fout.write(reverse_bits(corrected_data[config['metadata_size']:])) # write 1040 bytes
                                else:
                                    fout.write(reverse_bits(corrected_data[0:1008])) # пишем только данные без OOB т.е. 994 + 14 байт = 1008
                            else:
                                # TODO: need to fix here
                                fout.write(reverse_bits(corrected_data))

                            # increment good sector count
                            good_sector_count += 1

                            # clear bad sector flag
                            bad_sector = False

                            # sector had no bit errors
                            if corrected == 0:
                                uncorrected_sector_count += 1
                                # do not change my_page_mark
                            else:
                                corrected_sector_count += 1
                                my_page_mark += 1 # +1 fixed sector at this memory page

                            # early exit if we have a good sector
                            break

                        else:
                            print("[-] Error: Corrected data of sector {} has "
                                  "wrong size ({}) but need ({})"
                                  .format(sector, len(corrected_data), config['sectorsize']))
                            # set bad sector flag
                            bad_sector = True
                            my_page_mark += 0x100 # +100 if size is incorrect
                    else:
                        # set bad sector flag
                        bad_sector = True
                        my_page_mark += 0x10 # +10 if too much errors (more that ECC can correct)

                # check if the sector was corrupted in all input files
                if bad_sector:
                    # write corrupted sector data to output file
                    if sector == 0:
                        fout.write(reverse_bits(corrected_data))
                    else:
                        fout.write(reverse_bits(corrected_data[0:1008]))

                    # increment bad sector count
                    bad_sector_count += 1

                    # print("incorrectable:{} {} {}".format(block, page, sector))

            # число показывает сколько секторов в странице смогли исправить
            # если зеленое - то страница вся стала исправленой
            # если красное - то на странице есть сектора в которых ошибок слишком много и ECC не достаточно для фикса
            if my_page_mark == 0:
                if (page+1)%64 == 0:
                    print("\033[92m█\033[0m")  # no errors page symbol
                else:
                    print("\033[92m█\033[0m", end='')
            else:
                if my_page_mark < 0x10:
                    if (page+1)%64 == 0:
                        print("\033[32m%i\033[0m" % my_page_mark)  # low errors (fixed) page symbol
                    else:
                        print("\033[32m%i\033[0m" % my_page_mark, end='')
                else:
                    if my_page_mark >= 0x10:
                        if (page+1)%64 == 0:
                            print("\033[91m%i\033[0m" % (my_page_mark&0xF))  # too much errors (not fixed) page symbol
                        else:
                            print("\033[91m%i\033[0m" % (my_page_mark&0xF), end='')
                    else:
                        if my_page_mark >= 0x100:
                            if (page+1)%64 == 0:
                                print("\033[31mS\033[0m")  # incorrect size page symbol
                            else:
                                print("\033[31mS\033[0m", end='')

            my_page_mark = 0

        # show some statistics during processing all sectors
        #progress = processed_sector_count / total_sectors * 100
        #print("\r    Progress: {:.2f}% ({}/{} sectors)"
        #      .format(progress, processed_sector_count, total_sectors), end="")

    # close output file
    fout.close()

    # close memory-maps
    for mm in input_file_mmaps:
        mm.close()

    # close input files
    for f in input_file_handles:
        f.close()

    # show some statistics at the end
    good_sector_percentage = good_sector_count / total_sectors * 100
    bad_sector_percentage = bad_sector_count / total_sectors * 100
    corrected_sector_percentage = corrected_sector_count / total_sectors * 100
    uncorrected_sector_percentage = uncorrected_sector_count / total_sectors * 100
    blank_page_percentage = blank_page_count / total_page_count * 100
    blank_sector_count = blank_page_count * sectors_per_page
    blank_sector_percentage = blank_sector_count / total_sectors * 100
    good_data_sector_count = good_sector_count - blank_sector_count
    good_data_sector_percentage = good_data_sector_count / total_sectors * 100
    data_sector_count = good_data_sector_count + bad_sector_count
    data_sector_percentage = data_sector_count / total_sectors * 100

    print("\n[*] Completed error correcting process")
    print("    Successfully written {} bytes of data to output file '{}'"
          .format(config['sectorsize'] * total_sectors, outfile))
    print("    -----\n    Some statistics\n"
          "    Total pages:        {}\n"
          "    Blank pages:        {} ({:.2f}%)\n"
          "    Data pages:         {} ({:.2f}%)\n"
          "    -----\n    Total sectors:      {}\n"
          "    Valid sectors:      {} ({:.2f}%)\n"
          "    Corrupted sectors:  {} ({:.2f}%)\n"
          "    -----\n    Blank sectors:      {} ({:.2f}%)\n"
          "    Data sectors:       {} ({:.2f}%)\n"
          "    -----\n    Valid data sectors: {} ({:.2f}%)\n"
          "    Corrected sectors:  {} ({:.2f}%)\n"
          "    No errors sectors:  {} ({:.2f}%)\n"
          "    Bad marked blocks:  {}\n"
          "ECC length: {} bytes"
          .format(total_page_count,
                  blank_page_count, blank_page_percentage,
                  total_page_count - blank_page_count, 100 - blank_page_percentage,
                  total_sectors,
                  good_sector_count, good_sector_percentage,
                  bad_sector_count, bad_sector_percentage,
                  blank_sector_count, blank_sector_percentage,
                  data_sector_count, data_sector_percentage,
                  good_data_sector_count, good_data_sector_percentage,
                  corrected_sector_count, corrected_sector_percentage,
                  uncorrected_sector_count, uncorrected_sector_percentage,
                  bad_block_count,
                  bch.ecc_bytes))

    return


# ---------------------------------------------------------------------------
def atmel2_error_correction(infiles, outfile, config):
    """Do some error correction"""

    # ECC for blank sector (FF only)
    nullbyte_ecc1 = [0x10, 0xAE, 0xD1, 0xF6, 0x12, 0x6C, 0x65, 0x3D, 0x68, 0x86, 0x1A, 0xDB, 0x4A]

    # initialize BCH decoder
    bch = bchlib.BCH(config['ecc_errors'], config['ecc_polynom'])

    # open output file
    fout = open(outfile, "wb")

    # adjust data size depending on configured file offset
    data_size = config['filesize'] - config['file_offset']

    # initialize some variables
    processed_sector_count = 0
    corrected_sector_count = 0
    uncorrected_sector_count = 0
    good_sector_count = 0
    bad_sector_count = 0
    blank_page_count = 0
    bad_block_count = 0
    total_page_count = data_size // config['fullpagesize']
    sectors_per_page = config['sectors_per_page']
    total_sectors = total_page_count * sectors_per_page
    page_size = config['pagesize']
    block_size_bytes = config['fullpagesize'] * config['blocksize'] # размер одного блока памяти (2 112 байт * 64 = 135 168 байт)
    total_blocks = data_size // block_size_bytes
    
    #config['blocksize'] - балбесы! это оказывается = 64 - кол-во страниц в блоке

    block_offset = config['file_offset'] // block_size_bytes

    # zero page data
    zero_page = b'\x00'*config['pagesize'] + b'\xff'*2 + b'\x00'*(config['spareareasize'] - 2 - 6) + b'\xff'*6 # полностью пустая страница: данные 0000...00 потом FF FF (bad block marker) потом 14 байт 00 (т.к. ЕСС = 00) * 4 и FF FF FF FF FF FF (для дополнения до 64 байт OOB)
    blank_page = b'\xff'*config['fullpagesize']
    # А у страницы заполненной FF будет ЕСС = 10AED1F6126C653D68861ADB4A

    # bad block marker offsets - bad if not 0xFFFF
    bb_offset1 = config['pagesize'] # at first page: 0x800
    bb_offset2 = config['fullpagesize'] + config['pagesize'] # at second page

    # work with input files
    input_file_handles = []
    input_file_index = 0
    for f in infiles:
        input_file_handles.append(open(f, "r+b"))

    # memory-map the input files
    input_file_mmaps = []
    for fin in input_file_handles:
        input_file_mmaps.append(mmap.mmap(fin.fileno(), 0))

    # set current input file memory-map
    mm = input_file_mmaps[input_file_index]

    # with open(infile, "rb") as fin:
    print("[*] Starting error correcting process ...")

    for block in range(total_blocks): # берем каждый блок - от 0 да 1023 включительно
        # read current block data
        start_block = (block + block_offset) * block_size_bytes
        end_block = start_block + block_size_bytes
        block_data = mm[start_block:end_block]

        # check if page has Bad Block marker != FF FF
        # 1st page bad block marker
        b1 = block_data[bb_offset1:bb_offset1 + 2]
        # 2nd page bad block marker
        b2 = block_data[bb_offset2:bb_offset2 + 2]

        if b1 != b'\xff\xff' or b2 != b'\xff\xff':
            # increment bad block counter
            bad_block_count += 1
            continue

        # process pages in block
        for page in range(config['blocksize']): # берем каждую страницу взятого блока - от 0 да 63 включительно
            start_page = page * config['fullpagesize']
            end_page = start_page + config['fullpagesize']
            page_data = block_data[start_page:end_page] # get page: data(2048) + spare(64)

            # check if page is blank (00 00 00 ... 00 FF FF 00...00 FF FF FF FF FF FF)
            if page_data == zero_page or page_data == blank_page:
                # increment blank page counter
                blank_page_count += 1

                # increment good sector counter
                good_sector_count += sectors_per_page

                # increment count of processed sectors
                processed_sector_count += sectors_per_page

                # write blank page to output file
                if page_data == zero_page:
                    fout.write(zero_page[:page_size]) # запишем 2048 байт 0x00 раз у нас страница пустая
                else:
                    if page_data == blank_page:
                        fout.write(blank_page[:page_size]) # запишем 2048 байт 0xFF раз у нас страница стёртая

                # show some statistics during processing all sectors
                ##progress = processed_sector_count / total_sectors * 100
                ##print("\r    Progress: {:.2f}% ({}/{} sectors)".format(progress, processed_sector_count, total_sectors), end="")
                if (page+1)%64 == 0:
                    print("\033[90m█\033[0m") # blank page symbol
                else:
                    print("\033[90m█\033[0m", end='')
                continue

            # if page not blank start processing sectors in page
            my_page_mark = 0 # no errors at this sector
            
            
            # process sectors in page
            for sector in range(sectors_per_page):
                # initialize bad sector flag
                bad_sector = False

                # increment count of processed sectors
                processed_sector_count += 1

                # use all input files, if required (early exit condition)
                for mmin in input_file_mmaps:
                    # read page of current memory-map
                    start_page = start_block + page * config['fullpagesize']
                    end_page = start_page + config['fullpagesize']
                    page_data = mmin[start_page:end_page]
                    # FF page with valid ECC - for test only
                    #page_data = b'\xff'*config['pagesize'] + b'\xff'+b'\xff' + b'\x10' + b'\xAE' + b'\xD1' + b'\xF6' + b'\x12' + b'\x6C' + b'\x65' + b'\x3D' + b'\x68' + b'\x86' + b'\x1A' + b'\xDB' + b'\x4A' + b'\x00'+ b'\x10' + b'\xAE' + b'\xD1' + b'\xF6' + b'\x12' + b'\x6C' + b'\x65' + b'\x3D' + b'\x68' + b'\x86' + b'\x1A' + b'\xDB' + b'\x4A' + b'\x00'+ b'\x10' + b'\xAE' + b'\xD1' + b'\xF6' + b'\x12' + b'\x6C' + b'\x65' + b'\x3D' + b'\x68' + b'\x86' + b'\x1A' + b'\xDB' + b'\x4A' + b'\x00'+ b'\x10' + b'\xAE' + b'\xD1' + b'\xF6' + b'\x12' + b'\x6C' + b'\x65' + b'\x3D' + b'\x68' + b'\x86' + b'\x1A' + b'\xDB' + b'\x4A' + b'\x00' + b'\xff' + b'\xff' + b'\xff' + b'\xff' + b'\xff' + b'\xff'

                    # get data of current sector
                    start_data = sector * config['sectorsize']
                    end_data = start_data + config['sectorsize']
                    #sector_data = reverse_bits(page_data[start_data:end_data])
                    sector_data = page_data[start_data:end_data]

                    # get ECC of current sector
                    start_ecc = config['pagesize'] + config['ecc_offset'] + (config['ecc_bytes_per_sector'] + 1) * sector # config['ecc_bytes_per_sector'] - тут 14 байт надо пропускать а у нас ECC в 13 байтах + 00
                    end_ecc = start_ecc + int(config['ecc_bytes_per_sector'])
                    sector_ecc = (page_data[start_ecc:end_ecc])

                    # calculate ECC
                    # 10AED1F6126C653D68861ADB4A - 13 байт ECC для FF сектора в 512 байт
                    #calc_ecc = bch.encode(sector_data) # получим 13 байт
                    #print('calc: %02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X' % (calc_ecc[0],calc_ecc[1],calc_ecc[2],calc_ecc[3],calc_ecc[4],calc_ecc[5],calc_ecc[6],calc_ecc[7],calc_ecc[8],calc_ecc[9],calc_ecc[10],calc_ecc[11],calc_ecc[12]))
                    #print('fromfile: %02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X' % (sector_ecc[0],sector_ecc[1],sector_ecc[2],sector_ecc[3],sector_ecc[4],sector_ecc[5],sector_ecc[6],sector_ecc[7],sector_ecc[8],sector_ecc[9],sector_ecc[10],sector_ecc[11],sector_ecc[12]))
                    #exit(1)

                    # check that data is correct
                    if len(sector_data)*8 > (bch.n - bch.ecc_bits):
                        print("data len = %i should be <= ecc (n - bits) = %i - %i" % (len(sector_data)*8, bch.n, bch.ecc_bits))
                    if len(sector_ecc) != bch.ecc_bytes:
                        print("Error: file read ECC len=%i, conf ECC len=%i" % (len(sector_ecc), bch.ecc_bytes))
                    
                    # decrypt sector ECC with xor key
                    #decrypted_ecc = xor_crypto(sector_ecc, nullbyte_ecc1) #- не надо тут это делать т.к. у 000000 данных и ЕСС 00
            
                    # apply BCH error correcting code
                    #corrected = bch.decode(sector_data, reverse_bits(decrypted_ecc))
                    corrected = bch.decode(sector_data, sector_ecc)
                    
                    corrected_data = bytearray(sector_data)
                    corrected_ecc = bytearray(sector_ecc)
                    bch.correct(corrected_data, corrected_ecc)

                    if corrected >= 0 and corrected <= bch.t:
                        # correctable number of errors

                        if len(corrected_data) == config['sectorsize']:
                            # write corrected sector data to output file
                            #fout.write(reverse_bits(corrected_data))
                            fout.write((corrected_data))
                            
                            # increment good sector count
                            good_sector_count += 1

                            # clear bad sector flag
                            bad_sector = False

                            # sector had no bit errors
                            if corrected == 0:
                                uncorrected_sector_count += 1
                                #print('here3')
                                # do not change my_page_mark
                            else:
                                #print('here4')
                                corrected_sector_count += 1
                                my_page_mark += 1 # +1 fixed sector at this memory page

                            # early exit if we have a good sector
                            break

                        else:
                            print("[-] Error: Corrected data of sector {} has "
                                  "wrong size ({}) but need ({})"
                                  .format(sector, len(corrected_data), config['sectorsize']))
                            # set bad sector flag
                            bad_sector = True
                            my_page_mark += 0x100 # +100 if size is incorrect
                    else:
                        # первая страница блока памяти у секторов полностью стертых в FF имеет ЕСС = тоже FF
                        if page == 0 and corrected_data == b'\xff'*config['sectorsize'] and sector_ecc == b'\xff'*int(config['ecc_bytes_per_sector']):
                            #print('GEEEEEE')
                            # write corrected sector data to output file
                            #fout.write(reverse_bits(corrected_data))
                            fout.write((corrected_data))

                            # increment good sector count
                            good_sector_count += 1

                            # clear bad sector flag
                            bad_sector = False

                            # sector had no bit errors
                            uncorrected_sector_count += 1
                        else:
                            #print('here5')
                            # set bad sector flag
                            bad_sector = True
                            my_page_mark += 0x10 # +10 if too much errors (more that ECC can correct)
                    
                # check if the sector was corrupted in all input files
                if bad_sector:
                    # write corrupted sector data to output file
                    #fout.write(reverse_bits(corrected_data))
                    fout.write((corrected_data))

                    # increment bad sector count
                    bad_sector_count += 1

                    # print("incorrectable:{} {} {}".format(block, page, sector))

            # число показывает сколько секторов в странице смогли исправить
            # если зеленое - то страница вся стала исправленой
            # если красное - то на странице есть сектора в которых ошибок слишком много и ECC не достаточно для фикса
            if my_page_mark == 0:
                if (page+1)%64 == 0:
                    print("\033[92m█\033[0m")  # no errors page symbol
                else:
                    print("\033[92m█\033[0m", end='')
            else:
                if my_page_mark < 0x10:
                    if (page+1)%64 == 0:
                        print("\033[32m%i\033[0m" % my_page_mark)  # low errors (fixed) page symbol
                    else:
                        print("\033[32m%i\033[0m" % my_page_mark, end='')
                else:
                    if my_page_mark >= 0x10:
                        if (page+1)%64 == 0:
                            print("\033[91m%i\033[0m" % (my_page_mark&0xF))  # too much errors (not fixed) page symbol
                        else:
                            print("\033[91m%i\033[0m" % (my_page_mark&0xF), end='')
                    else:
                        if my_page_mark >= 0x100:
                            if (page+1)%64 == 0:
                                print("\033[31mS\033[0m")  # incorrect size page symbol
                            else:
                                print("\033[31mS\033[0m", end='')

            my_page_mark = 0

        # show some statistics during processing all sectors
        #progress = processed_sector_count / total_sectors * 100
        #print("\r    Progress: {:.2f}% ({}/{} sectors)"
        #      .format(progress, processed_sector_count, total_sectors), end="")

    # close output file
    fout.close()

    # close memory-maps
    for mm in input_file_mmaps:
        mm.close()

    # close input files
    for f in input_file_handles:
        f.close()

    # show some statistics at the end
    good_sector_percentage = good_sector_count / total_sectors * 100
    bad_sector_percentage = bad_sector_count / total_sectors * 100
    corrected_sector_percentage = corrected_sector_count / total_sectors * 100
    uncorrected_sector_percentage = uncorrected_sector_count / total_sectors * 100
    blank_page_percentage = blank_page_count / total_page_count * 100
    blank_sector_count = blank_page_count * sectors_per_page
    blank_sector_percentage = blank_sector_count / total_sectors * 100
    good_data_sector_count = good_sector_count - blank_sector_count
    good_data_sector_percentage = good_data_sector_count / total_sectors * 100
    data_sector_count = good_data_sector_count + bad_sector_count
    data_sector_percentage = data_sector_count / total_sectors * 100

    print("\n[*] Completed error correcting process")
    print("    Successfully written {} bytes of data to output file '{}'"
          .format(config['sectorsize'] * total_sectors, outfile))
    print("    -----\n    Some statistics\n"
          "    Total pages:        {}\n"
          "    Blank pages:        {} ({:.2f}%)\n"
          "    Data pages:         {} ({:.2f}%)\n"
          "    -----\n    Total sectors:      {}\n"
          "    Valid sectors:      {} ({:.2f}%)\n"
          "    Corrupted sectors:  {} ({:.2f}%)\n"
          "    -----\n    Blank sectors:      {} ({:.2f}%)\n"
          "    Data sectors:       {} ({:.2f}%)\n"
          "    -----\n    Valid data sectors: {} ({:.2f}%)\n"
          "    Corrected sectors:  {} ({:.2f}%)\n"
          "    No errors sectors:  {} ({:.2f}%)\n"
          "    Bad marked blocks:  {}\n"
          "ECC length: {} bytes"
          .format(total_page_count,
                  blank_page_count, blank_page_percentage,
                  total_page_count - blank_page_count, 100 - blank_page_percentage,
                  total_sectors,
                  good_sector_count, good_sector_percentage,
                  bad_sector_count, bad_sector_percentage,
                  blank_sector_count, blank_sector_percentage,
                  data_sector_count, data_sector_percentage,
                  good_data_sector_count, good_data_sector_percentage,
                  corrected_sector_count, corrected_sector_percentage,
                  uncorrected_sector_count, uncorrected_sector_percentage,
                  bad_block_count,
                  bch.ecc_bytes))

    return
# --------------------------------------------------------------------------------------------------

def some_error_correction(infiles, outfile, config):
    """Do some error correction"""

    # ECC for blank sector (FF only)
    #nullbyte_ecc1 = [0x10, 0xAE, 0xD1, 0xF6, 0x12, 0x6C, 0x65, 0x3D, 0x68, 0x86, 0x1A, 0xDB, 0x4A]

    # initialize BCH decoder
    bch = bchlib.BCH(config['ecc_errors'], config['ecc_polynom'])

    # open output file
    fout = open(outfile, "wb")

    # adjust data size depending on configured file offset
    data_size = config['filesize'] - config['file_offset']

    # initialize some variables
    processed_sector_count = 0
    corrected_sector_count = 0
    uncorrected_sector_count = 0
    good_sector_count = 0
    bad_sector_count = 0
    blank_page_count = 0
    bad_block_count = 0
    total_page_count = data_size // config['fullpagesize']
    sectors_per_page = config['sectors_per_page']
    total_sectors = total_page_count * sectors_per_page
    page_size = config['pagesize']
    block_size_bytes = config['fullpagesize'] * config['blocksize'] # размер одного блока памяти (2 112 байт * 64 = 135 168 байт)
    total_blocks = data_size // block_size_bytes
    
    #config['blocksize'] - балбесы! это оказывается = 64 - кол-во страниц в блоке

    block_offset = config['file_offset'] // block_size_bytes

    # zero page data
    #zero_page = b'\x00'*config['pagesize'] + b'\xff'*2 + b'\x00'*(config['spareareasize'] - 2 - 6) + b'\xff'*6 # полностью пустая страница: данные 0000...00 потом FF FF (bad block marker) потом 14 байт 00 (т.к. ЕСС = 00) * 4 и FF FF FF FF FF FF (для дополнения до 64 байт OOB)
    blank_page = b'\xff'*config['fullpagesize'] # вся страница и OOB область полностью из FF
    blank_page2 = b'\xff'*config['pagesize'] + b'\xff'*8 + b'\x69\x77\x6C\x75\x66\x6D\x62\x70' + b'\xff'*48 # а есть страницы с "ÿiwlufmbp" текстом в OOB и остальное так же FF - они расположены в начале блока
    # А у страницы заполненной FF будет ЕСС = 0D 7E C3 3C 66 95 38
    #sector_data= b'\xff'*512  # валидный ECC = [0x0D, 0x7E, 0xC3, 0x3C, 0x66, 0x95, 0x38]
    #sector_data= b'\x00'*512  # валидный ECC = [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]


    # bad block marker offsets - bad if not 0xFFFF
    bb_offset1 = config['pagesize'] # at first page: 0x800
    bb_offset2 = config['fullpagesize'] + config['pagesize'] # at second page

    # work with input files
    input_file_handles = []
    input_file_index = 0
    for f in infiles:
        input_file_handles.append(open(f, "r+b"))

    # memory-map the input files
    input_file_mmaps = []
    for fin in input_file_handles:
        input_file_mmaps.append(mmap.mmap(fin.fileno(), 0))

    # set current input file memory-map
    mm = input_file_mmaps[input_file_index]

    # with open(infile, "rb") as fin:
    print("[*] Starting error correcting process ...")

    for block in range(total_blocks): # берем каждый блок - от 0 да 1023 включительно
        # read current block data
        start_block = (block + block_offset) * block_size_bytes
        end_block = start_block + block_size_bytes
        block_data = mm[start_block:end_block]

        # check if page has Bad Block marker != FF FF
        # 1st page bad block marker
        b1 = block_data[bb_offset1:bb_offset1 + 2]
        # 2nd page bad block marker
        b2 = block_data[bb_offset2:bb_offset2 + 2]

        if b1 != b'\xff\xff' or b2 != b'\xff\xff':
            # increment bad block counter
            bad_block_count += 1
            continue

        # process pages in block
        for page in range(config['blocksize']): # берем каждую страницу взятого блока - от 0 да 63 включительно
            start_page = page * config['fullpagesize']
            end_page = start_page + config['fullpagesize']
            page_data = block_data[start_page:end_page] # get page: data(2048) + spare(64)

            # check if all page is blank (FF FF FF FF FF FF)
            if page_data == blank_page or page_data == blank_page2:
                # increment blank page counter
                blank_page_count += 1

                # increment good sector counter
                good_sector_count += sectors_per_page

                # increment count of processed sectors
                processed_sector_count += sectors_per_page

                # write blank page to output file
                if page_data == blank_page:
                    fout.write(blank_page[:page_size]) # запишем 2048 байт 0xFF раз у нас страница стёртая

                # show some statistics during processing all sectors
                ##progress = processed_sector_count / total_sectors * 100
                ##print("\r    Progress: {:.2f}% ({}/{} sectors)".format(progress, processed_sector_count, total_sectors), end="")
                if (page+1)%64 == 0:
                    print("\033[90m█\033[0m") # blank page symbol
                else:
                    print("\033[90m█\033[0m", end='')
                continue

            # if page not blank start processing sectors in page
            my_page_mark = 0 # no errors at this sector
            
            
            # process sectors in page
            for sector in range(sectors_per_page):
                # initialize bad sector flag
                bad_sector = False

                # increment count of processed sectors
                processed_sector_count += 1

                # use all input files, if required (early exit condition)
                for mmin in input_file_mmaps:
                    # read page of current memory-map
                    start_page = start_block + page * config['fullpagesize']
                    end_page = start_page + config['fullpagesize']
                    page_data = mmin[start_page:end_page]
                    # FF page with valid ECC - for test only
                    #page_data = b'\xff'*config['pagesize'] + b'\xff'+b'\xff' + b'\x10' + b'\xAE' + b'\xD1' + b'\xF6' + b'\x12' + b'\x6C' + b'\x65' + b'\x3D' + b'\x68' + b'\x86' + b'\x1A' + b'\xDB' + b'\x4A' + b'\x00'+ b'\x10' + b'\xAE' + b'\xD1' + b'\xF6' + b'\x12' + b'\x6C' + b'\x65' + b'\x3D' + b'\x68' + b'\x86' + b'\x1A' + b'\xDB' + b'\x4A' + b'\x00'+ b'\x10' + b'\xAE' + b'\xD1' + b'\xF6' + b'\x12' + b'\x6C' + b'\x65' + b'\x3D' + b'\x68' + b'\x86' + b'\x1A' + b'\xDB' + b'\x4A' + b'\x00'+ b'\x10' + b'\xAE' + b'\xD1' + b'\xF6' + b'\x12' + b'\x6C' + b'\x65' + b'\x3D' + b'\x68' + b'\x86' + b'\x1A' + b'\xDB' + b'\x4A' + b'\x00' + b'\xff' + b'\xff' + b'\xff' + b'\xff' + b'\xff' + b'\xff'

                    # get data of current sector
                    start_data = sector * config['sectorsize']
                    end_data = start_data + config['sectorsize']
                    #sector_data = reverse_bits(page_data[start_data:end_data])
                    sector_data = page_data[start_data:end_data]

                    # get ECC of current sector
                    start_ecc = config['pagesize'] + config['ecc_offset'] + (config['ecc_bytes_per_sector']) * sector
                    end_ecc = start_ecc + int(config['ecc_bytes_per_sector'])
                    sector_ecc = (page_data[start_ecc:end_ecc])

                    # calculate ECC
                    # 10AED1F6126C653D68861ADB4A - 13 байт ECC для FF сектора в 512 байт
                    #calc_ecc = bch.encode(sector_data) # получим 13 байт
                    #print('calc: %02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X' % (calc_ecc[0],calc_ecc[1],calc_ecc[2],calc_ecc[3],calc_ecc[4],calc_ecc[5],calc_ecc[6],calc_ecc[7],calc_ecc[8],calc_ecc[9],calc_ecc[10],calc_ecc[11],calc_ecc[12]))
                    #print('fromfile: %02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X' % (sector_ecc[0],sector_ecc[1],sector_ecc[2],sector_ecc[3],sector_ecc[4],sector_ecc[5],sector_ecc[6],sector_ecc[7],sector_ecc[8],sector_ecc[9],sector_ecc[10],sector_ecc[11],sector_ecc[12]))
                    #exit(1)

                    # check that data is correct
                    if len(sector_data)*8 > (bch.n - bch.ecc_bits):
                        print("data len = %i should be <= ecc (n - bits) = %i - %i" % (len(sector_data)*8, bch.n, bch.ecc_bits))
                    if len(sector_ecc) != bch.ecc_bytes:
                        print("Error: file read ECC len=%i, conf ECC len=%i" % (len(sector_ecc), bch.ecc_bytes))
                    
                    # decrypt sector ECC with xor key
                    #decrypted_ecc = xor_crypto(sector_ecc, nullbyte_ecc1) #- не надо тут это делать т.к. у 000000 данных и ЕСС 00
            
                    # apply BCH error correcting code
                    #calc_ecc = (bch.encode((sector_data))) # получим массив байт
                    #calc_ecc = bytes.fromhex("0" + calc_ecc.hex()[:-1]) # shift ecc value >>4
                    sector_ecc = bytes.fromhex(sector_ecc.hex()[1:] + "0") # shift ecc value <<4 когда считаем из файла надо будет сдвинуть перед BCH.decode()

                    corrected = bch.decode(sector_data, sector_ecc)
                    
                    corrected_data = bytearray(sector_data)
                    corrected_ecc = bytearray(sector_ecc)
                    bch.correct(corrected_data, corrected_ecc)

                    if corrected >= 0 and corrected <= bch.t:
                        # correctable number of errors

                        if len(corrected_data) == config['sectorsize']:
                            # write corrected sector data to output file
                            #fout.write(reverse_bits(corrected_data))
                            fout.write((corrected_data))
                            
                            # increment good sector count
                            good_sector_count += 1

                            # clear bad sector flag
                            bad_sector = False

                            # sector had no bit errors
                            if corrected == 0:
                                uncorrected_sector_count += 1
                                #print('here3')
                                # do not change my_page_mark
                            else:
                                #print('here4')
                                corrected_sector_count += 1
                                my_page_mark += 1 # +1 fixed sector at this memory page

                            # early exit if we have a good sector
                            break

                        else:
                            print("[-] Error: Corrected data of sector {} has "
                                  "wrong size ({}) but need ({})"
                                  .format(sector, len(corrected_data), config['sectorsize']))
                            # set bad sector flag
                            bad_sector = True
                            my_page_mark += 0x100 # +100 if size is incorrect
                    else:
                        #print('here5')
                        # set bad sector flag
                        bad_sector = True
                        my_page_mark += 0x10 # +10 if too much errors (more that ECC can correct)
                    
                # check if the sector was corrupted in all input files
                if bad_sector:
                    # write corrupted sector data to output file
                    #fout.write(reverse_bits(corrected_data))
                    fout.write((corrected_data))

                    # increment bad sector count
                    bad_sector_count += 1

                    # print("incorrectable:{} {} {}".format(block, page, sector))

            # число показывает сколько секторов в странице смогли исправить
            # если зеленое - то страница вся стала исправленой
            # если красное - то на странице есть сектора в которых ошибок слишком много и ECC не достаточно для фикса
            if my_page_mark == 0:
                if (page+1)%64 == 0:
                    print("\033[92m█\033[0m")  # no errors page symbol
                else:
                    print("\033[92m█\033[0m", end='')
            else:
                if my_page_mark < 0x10:
                    if (page+1)%64 == 0:
                        print("\033[32m%i\033[0m" % my_page_mark)  # low errors (fixed) page symbol
                    else:
                        print("\033[32m%i\033[0m" % my_page_mark, end='')
                else:
                    if my_page_mark >= 0x10:
                        if (page+1)%64 == 0:
                            print("\033[91m%i\033[0m" % (my_page_mark&0xF))  # too much errors (not fixed) page symbol
                        else:
                            print("\033[91m%i\033[0m" % (my_page_mark&0xF), end='')
                    else:
                        if my_page_mark >= 0x100:
                            if (page+1)%64 == 0:
                                print("\033[31mS\033[0m")  # incorrect size page symbol
                            else:
                                print("\033[31mS\033[0m", end='')

            my_page_mark = 0

        # show some statistics during processing all sectors
        #progress = processed_sector_count / total_sectors * 100
        #print("\r    Progress: {:.2f}% ({}/{} sectors)"
        #      .format(progress, processed_sector_count, total_sectors), end="")

    # close output file
    fout.close()

    # close memory-maps
    for mm in input_file_mmaps:
        mm.close()

    # close input files
    for f in input_file_handles:
        f.close()

    # show some statistics at the end
    good_sector_percentage = good_sector_count / total_sectors * 100
    bad_sector_percentage = bad_sector_count / total_sectors * 100
    corrected_sector_percentage = corrected_sector_count / total_sectors * 100
    uncorrected_sector_percentage = uncorrected_sector_count / total_sectors * 100
    blank_page_percentage = blank_page_count / total_page_count * 100
    blank_sector_count = blank_page_count * sectors_per_page
    blank_sector_percentage = blank_sector_count / total_sectors * 100
    good_data_sector_count = good_sector_count - blank_sector_count
    good_data_sector_percentage = good_data_sector_count / total_sectors * 100
    data_sector_count = good_data_sector_count + bad_sector_count
    data_sector_percentage = data_sector_count / total_sectors * 100

    print("\n[*] Completed error correcting process")
    print("    Successfully written {} bytes of data to output file '{}'"
          .format(config['sectorsize'] * total_sectors, outfile))
    print("    -----\n    Some statistics\n"
          "    Total pages:        {}\n"
          "    Blank pages:        {} ({:.2f}%)\n"
          "    Data pages:         {} ({:.2f}%)\n"
          "    -----\n    Total sectors:      {}\n"
          "    Valid sectors:      {} ({:.2f}%)\n"
          "    Corrupted sectors:  {} ({:.2f}%)\n"
          "    -----\n    Blank sectors:      {} ({:.2f}%)\n"
          "    Data sectors:       {} ({:.2f}%)\n"
          "    -----\n    Valid data sectors: {} ({:.2f}%)\n"
          "    Corrected sectors:  {} ({:.2f}%)\n"
          "    No errors sectors:  {} ({:.2f}%)\n"
          "    Bad marked blocks:  {}\n"
          "ECC length: {} bytes"
          .format(total_page_count,
                  blank_page_count, blank_page_percentage,
                  total_page_count - blank_page_count, 100 - blank_page_percentage,
                  total_sectors,
                  good_sector_count, good_sector_percentage,
                  bad_sector_count, bad_sector_percentage,
                  blank_sector_count, blank_sector_percentage,
                  data_sector_count, data_sector_percentage,
                  good_data_sector_count, good_data_sector_percentage,
                  corrected_sector_count, corrected_sector_percentage,
                  uncorrected_sector_count, uncorrected_sector_percentage,
                  bad_block_count,
                  bch.ecc_bytes))

    return
# --------------------------------------------------------------------------------------------------




def atmel_error_correction(infiles, outfile, config):
    """Do some error correction"""

    # initialize BCH decoder
    bch = bchlib.BCH(config['ecc_errors'], config['ecc_polynom'])

    # open output file
    fout = open(outfile, "wb")

    # initialize some variables
    processed_sector_count = 0
    corrected_sector_count = 0
    uncorrected_sector_count = 0
    good_sector_count = 0
    bad_sector_count = 0
    blank_page_count = 0
    bad_block_count = 0
    total_page_count = config['filesize'] // config['fullpagesize']
    sectors_per_page = config['pagesize'] // config['sectorsize']
    total_sectors = total_page_count * sectors_per_page
    page_size = config['pagesize']
    block_size_bytes = config['fullpagesize'] * config['blocksize']
    total_blocks = config['filesize'] // block_size_bytes

    # blank page data
    blank_page = b'\xff' * config['fullpagesize']

    # bad block offsets (ATMEL)
    bb_offset1 = config['pagesize']
    bb_offset2 = config['fullpagesize'] + config['pagesize']
    bb_offset3 = (config['blocksize'] - 1) * config['fullpagesize'] + \
        config['pagesize']

    # work with input files
    input_file_handles = []
    input_file_index = 0
    for f in infiles:
        input_file_handles.append(open(f, "r+b"))

    # memory-map the input files
    input_file_mmaps = []
    for fin in input_file_handles:
        input_file_mmaps.append(mmap.mmap(fin.fileno(), 0))

    # set current input file memory-map
    mm = input_file_mmaps[input_file_index]

    # search for XOR key used for "encrypting" ECCs in the spare area
    nullbyte_sector = b'\x00' * config['sectorsize']
    print("[*] Search for ECC crypto key ...")
    key_found = False

    for block in range(total_blocks):
        start_block = block * block_size_bytes
        end_block = start_block + block_size_bytes
        block_data = mm[start_block:end_block]

        # 1st page bad block marker
        b1 = block_data[bb_offset1]

        # 2nd page bad block marker
        b2 = block_data[bb_offset2]

        # last page bad block marker
        b3 = block_data[bb_offset3]

        if b1 != 0xff or b2 != 0xff or b3 != 0xff:
            # increment bad block counter
            bad_block_count += 1
            continue

        for page in range(config['blocksize']):
            start_page = page * config['fullpagesize']
            end_page = start_page + config['fullpagesize']
            page_data = block_data[start_page:end_page] # get page - data(2048) + spare(64)

            # check if page is blank
            if page_data == blank_page:
                continue

            # process sectors in page
            for sector in range(sectors_per_page):
                # get data of current sector
                start_data = sector * config['sectorsize']
                end_data = start_data + config['sectorsize']
                sector_data = reverse_bits(page_data[start_data:end_data])

                if sector_data == nullbyte_sector:
                    # get ECC of the found nullbyte sector
                    start_ecc = config['pagesize'] + config['ecc_offset'] + \
                        config['ecc_bytes_per_sector'] * sector
                    end_ecc = start_ecc + config['ecc_bytes_per_sector']
                    nullbyte_ecc = page_data[start_ecc:end_ecc]

                    # we have found our xor key, so stop searching
                    print("[*] Found at 0x%08X ECC crypto key: {}".format(nullbyte_ecc.hex()) % (start_block + start_page + start_data))
                    key_found = True
                    break

            # if the XOR key was found, stop searching
            if key_found:
                break

        # if the XOR key was found, stop searching
        if key_found:
            break

    if not key_found:
        print("[-] Could not find the ECC crypto key. Please use other dump files")
        sys.exit(1)

    # with open(infile, "rb") as fin:
    print("[*] Starting error correcting process ...")

    for block in range(total_blocks):
        # read current block data
        # fin.seek(block * block_size_bytes)
        # block_data = fin.read(block_size_bytes)

        start_block = block * block_size_bytes
        end_block = start_block + block_size_bytes
        block_data = mm[start_block:end_block]

        # 1st page bad block marker
        b1 = block_data[bb_offset1]

        # 2nd page bad block marker
        b2 = block_data[bb_offset2]

        # last page bad block marker
        b3 = block_data[bb_offset3]

        if b1 != 0xff or b2 != 0xff or b3 != 0xff:
            # increment bad block counter
            bad_block_count += 1
            continue

        # process pages in block
        for page in range(config['blocksize']):
            start_page = page * config['fullpagesize']
            end_page = start_page + config['fullpagesize']
            page_data = block_data[start_page:end_page]

            # check if page is blank
            if page_data == blank_page:
                # increment blank page counter
                blank_page_count += 1

                # increment good sector counter
                good_sector_count += sectors_per_page

                # increment count of processed sectors
                processed_sector_count += sectors_per_page

                # write blank page to output file
                fout.write(blank_page[:page_size])

                # show some statistics during processing all sectors
                progress = processed_sector_count / total_sectors * 100
                print("\r    Progress: {:.2f}% ({}/{} sectors)"
                      .format(progress, processed_sector_count,
                              total_sectors), end="")
                continue

            # process sectors in page
            for sector in range(sectors_per_page):
                # initialize bad sector flag
                bad_sector = False

                # increment count of processed sectors
                processed_sector_count += 1

                # use all input files, if required (early exit condition)
                for mmin in input_file_mmaps:
                    # read page of current memory-map
                    start_page = start_block + page * config['fullpagesize']
                    end_page = start_page + config['fullpagesize']
                    page_data = mmin[start_page:end_page]

                    # get ECC of current sector
                    start_ecc = config['pagesize'] + config['ecc_offset'] + \
                        config['ecc_bytes_per_sector'] * sector
                    end_ecc = start_ecc + config['ecc_bytes_per_sector']
                    sector_ecc = page_data[start_ecc:end_ecc]

                    # get data of current sector
                    start_data = sector * config['sectorsize']
                    end_data = start_data + config['sectorsize']
                    sector_data = reverse_bits(page_data[start_data:end_data])

                    # calculate ECC
                    # calc_ecc = reverse_bits(bch.encode(sector_data))

                    # decrypt sector ECC with xor key
                    decrypted_sector_ecc = xor_crypto(sector_ecc, nullbyte_ecc)

                    # apply BCH error correcting code
                    corrected = bch.decode(sector_data, reverse_bits(decrypted_sector_ecc))

                    corrected_data = bytearray(sector_data)
                    corrected_ecc = bytearray(sector_ecc)
                    bch.correct(corrected_data, corrected_ecc)

                    if corrected >= 0 and corrected <= bch.t:
                        # correctable number of errors

                        if len(corrected_data) == config['sectorsize']:
                            # write corrected sector data to output file
                            fout.write(reverse_bits(corrected_data))

                            # increment good sector count
                            good_sector_count += 1

                            # clear bad sector flag
                            bad_sector = False

                            # sector had no bit errors
                            if corrected == 0:
                                uncorrected_sector_count += 1
                            else:
                                corrected_sector_count += 1

                            # early exit if we have a good sector
                            break

                        else:
                            print("[-] Error: Corrected data of sector {} has "
                                  "wrong size ({})"
                                  .format(sector, config['sectorsize']))
                            # set bad sector flag
                            bad_sector = True
                    else:
                        # set bad sector flag
                        bad_sector = True

                # check if the sector was corrupted in all input files
                if bad_sector:
                    # write corrupted sector data to output file
                    fout.write(reverse_bits(corrected_data))

                    # increment bad sector count
                    bad_sector_count += 1

                    # print("incorrectable:{} {} {}".format(block, page, sector))

        # show some statistics during processing all sectors
        progress = processed_sector_count / total_sectors * 100
        print("\r    Progress: {:.2f}% ({}/{} sectors)"
              .format(progress, processed_sector_count, total_sectors), end="")

    # close output file
    fout.close()

    # close memory-maps
    for mm in input_file_mmaps:
        mm.close()

    # close input files
    for f in input_file_handles:
        f.close()

    # show some statistics at the end
    good_sector_percentage = good_sector_count / total_sectors * 100
    bad_sector_percentage = bad_sector_count / total_sectors * 100
    corrected_sector_percentage = corrected_sector_count / total_sectors * 100
    blank_page_percentage = blank_page_count / total_page_count * 100
    blank_sector_count = blank_page_count * sectors_per_page
    blank_sector_percentage = blank_sector_count / total_sectors * 100
    good_data_sector_count = good_sector_count - blank_sector_count
    good_data_sector_percentage = good_data_sector_count / total_sectors * 100
    data_sector_count = good_data_sector_count + bad_sector_count
    data_sector_percentage = data_sector_count / total_sectors * 100

    print("\n[*] Completed error correcting process")
    print("    Successfully written {} bytes of data to output file '{}'"
          .format(config['sectorsize'] * total_sectors, outfile))
    print("    -----\n    Some statistics\n"
          "    Total pages:        {}\n"
          "    Blank pages:        {} ({:.2f}%)\n"
          "    Blank sectors:      {} ({:.2f}%)\n"
          "    Data sectors:       {} ({:.2f}%)\n"
          "    Total sectors:      {}\n"
          "    Valid sectors:      {} ({:.2f}%)\n"
          "    Valid data sectors: {} ({:.2f}%)\n"
          "    Corrupted sectors:  {} ({:.2f}%)\n"
          "    Corrected sectors:  {} ({:.2f}%)\n"
          "    Bad blocks:         {}"
          .format(total_page_count, blank_page_count, blank_page_percentage,
                  blank_sector_count, blank_sector_percentage,
                  data_sector_count, data_sector_percentage,
                  total_sectors, good_sector_count, good_sector_percentage,
                  good_data_sector_count, good_data_sector_percentage,
                  bad_sector_count, bad_sector_percentage,
                  corrected_sector_count, corrected_sector_percentage,
                  bad_block_count))


def read_nxp_imx28_config(page, config):
    """Read NXP firmware control block (FCB) configuration from first page

       FCB is described in section 12.12.1.13 of the i.MX28 Reference Manual
       http://cache.freescale.com/files/dsp/doc/ref_manual/MCIMX28RM.pdf
    """

    # read FCB
    fcb = page[12:12 + 512]

    # read FCB structure (from data sheet MCIMX28RM)
    checksum = struct.unpack("<L", fcb[:4])[0]
    fingerprint = fcb[4:8]

    # check FCB fingerprint
    if fingerprint != b'FCB ':
        print("[-] Error: NXP firmware control block (FCB) incorrect")
        sys.exit(1)

    version = struct.unpack("<L", fcb[8:12])[0]
    nand_timing = struct.unpack("<Q", fcb[12:20])[0]
    pagesize = struct.unpack("<L", fcb[20:24])[0]
    totalpagesize = struct.unpack("<L", fcb[24:28])[0]
    pages_per_block = struct.unpack("<L", fcb[28:32])[0]
    number_of_nands = struct.unpack("<L", fcb[32:36])[0]
    number_of_dies = struct.unpack("<L", fcb[36:40])[0]
    cell_type = struct.unpack("<L", fcb[40:44])[0]
    ecc_block_n_type = struct.unpack("<L", fcb[44:48])[0]
    ecc_block_0_size = struct.unpack("<L", fcb[48:52])[0]
    ecc_block_n_size = struct.unpack("<L", fcb[52:56])[0]
    ecc_block_0_type = struct.unpack("<L", fcb[56:60])[0]
    metadata_bytes = struct.unpack("<L", fcb[60:64])[0]
    ecc_blocks_per_page = struct.unpack("<L", fcb[64:68])[0]
    firmware1_start_sector = struct.unpack("<L", fcb[104:108])[0]
    firmware2_start_sector = struct.unpack("<L", fcb[108:112])[0]
    firmware1_sectors = struct.unpack("<L", fcb[112:116])[0]
    firmware2_sectors = struct.unpack("<L", fcb[116:120])[0]
    dbbt_search_start = struct.unpack("<L", fcb[120:124])[0]
    bad_block_marker_byte = struct.unpack("<L", fcb[124:128])[0]
    bad_block_marker_start_bit = struct.unpack("<L", fcb[128:132])[0]
    bad_block_marker_offset = struct.unpack("<L", fcb[132:136])[0]

    # standards are great, everyone has his own
    # convert NXP config parameters to our tool config parameters
    config['pagesize'] = pagesize
    config['fullpagesize'] = totalpagesize
    config['blocksize'] = pages_per_block
    config['sectorsize'] = ecc_block_0_size
    config['ecc_errors'] = ecc_block_0_type * 2
    config['sectorsize0'] = ecc_block_0_size
    config['ecc_errors0'] = ecc_block_0_type * 2
    config['sectorsizeN'] = ecc_block_n_size
    config['ecc_errorsN'] = ecc_block_n_type * 2
    config['metadata_size'] = metadata_bytes
    config['useecc'] = True
    config['ecc_offset'] = 0
    config['ecc_bytes_per_sector'] = config['ecc_errors0'] * 13 / 8.
    config['ecc_bytes_per_sector0'] = config['ecc_errors0'] * 13 / 8.
    config['ecc_bytes_per_sectorN'] = config['ecc_errorsN'] * 13 / 8.
    config['sectors_per_page'] = ecc_blocks_per_page + 1
    config['spareareasize'] = totalpagesize - pagesize

    return config


def nxp_imx28_error_correction(infiles, outfile, config):
    """Do some error correction"""

    # initialize BCH decoder
    bch = bchlib.BCH(config['ecc_errors'], config['ecc_polynom'])

    # open output file
    fout = open(outfile, "wb")

    # adjust data size depending on configured file offset
    data_size = config['filesize'] - config['file_offset']

    # initialize some variables
    processed_sector_count = 0
    corrected_sector_count = 0
    uncorrected_sector_count = 0
    good_sector_count = 0
    bad_sector_count = 0
    blank_page_count = 0
    bad_block_count = 0
    sectors_per_page = config['sectors_per_page']
    total_page_count = data_size // config['fullpagesize']
    total_sectors = total_page_count * sectors_per_page
    page_size = config['pagesize']
    block_size_bytes = config['fullpagesize'] * config['blocksize']
    total_blocks = data_size // block_size_bytes

    block_offset = config['file_offset'] // block_size_bytes

    # blank page data
    blank_page = b'\xff' * config['fullpagesize']

    # work with input files
    input_file_handles = []
    input_file_index = 0
    for f in infiles:
        input_file_handles.append(open(f, "r+b"))

    # memory-map the input files
    input_file_mmaps = []
    for fin in input_file_handles:
        input_file_mmaps.append(mmap.mmap(fin.fileno(), 0))

    # set current input file memory-map
    mm = input_file_mmaps[input_file_index]

    # with open(infile, "rb") as fin:
    print("[*] Starting error correcting process ...")

    print("total_blocks=", block_size_bytes)
    
    for block in range(total_blocks):
        # read current block data
        start_block = (block + block_offset) * block_size_bytes
        end_block = start_block + block_size_bytes
        block_data = mm[start_block:end_block]

        # if block_data[0] != 0xff:
        #     # increment bad block counter
        #     bad_block_count += 1
        #     continue

        # process pages in block
        for page in range(config['blocksize']):
            start_page = page * config['fullpagesize']
            end_page = start_page + config['fullpagesize']
            page_data = block_data[start_page:end_page]

            # check if page is blank
            if page_data == blank_page:
                # increment blank page counter
                blank_page_count += 1

                # increment good sector counter
                good_sector_count += sectors_per_page

                # increment count of processed sectors
                processed_sector_count += sectors_per_page

                # write blank page to output file
                fout.write(blank_page[:page_size])

                # show some statistics during processing all sectors
                progress = processed_sector_count / total_sectors * 100
                print("\r    Progress: {:.2f}% ({}/{} sectors)"
                      .format(progress, processed_sector_count,
                              total_sectors), end="")
                continue

            # process sectors in page

            # process sector 0 (can be different than remaining sectors
            # of the current page)
            sector = 0
            bad_sector = False

            # increment count of processed sectors
            processed_sector_count += 1

            # use all input files, if required (early exit condition)
            for mmin in input_file_mmaps:
                # read page of current memory-map
                start_page = start_block + page * config['fullpagesize']
                end_page = start_page + config['fullpagesize']
                page_data = mmin[start_page:end_page]

                # get ECC of current sector
                start_ecc = config['sectorsize'] + config['metadata_size']
                end_ecc = start_ecc + int(config['ecc_bytes_per_sector'])
                sector_ecc = reverse_bits(page_data[start_ecc:end_ecc])

                # get data of current sector
                start_data = 0
                end_data = start_data + config['sectorsize'] + \
                           config['metadata_size']
                sector_data = reverse_bits(page_data[start_data:end_data])

                # # calculate ECC
                # calc_ecc = bch.encode(sector_data)

                # apply BCH error correcting code
                corrected = bch.decode(sector_data, sector_ecc)

                corrected_data = bytearray(sector_data)
                corrected_ecc = bytearray(sector_ecc)
                bch.correct(corrected_data, corrected_ecc)

                # set expected size for sector 0
                expected_size = config['sectorsize'] + config['metadata_size']

                if corrected >= 0 and corrected <= bch.t:
                    # correctable number of errors

                    if len(corrected_data) == expected_size:
                        # write corrected sector data to output file
                        # skip metadata for first sector in page
                        fout.write(reverse_bits(corrected_data[config['metadata_size']:]))

                        # increment good sector count
                        good_sector_count += 1

                        # clear bad sector flag
                        bad_sector = False

                        # sector had no bit errors
                        if corrected == 0:
                            uncorrected_sector_count += 1
                        else:
                            corrected_sector_count += 1

                        # early exit if we have a good sector
                        break

                    else:
                        print("[-] Error: Corrected data of sector {} has "
                              "wrong size ({})"
                              .format(sector, config['sectorsize']))
                        # set bad sector flag
                        bad_sector = True
                else:
                    # set bad sector flag
                    bad_sector = True

            # check if the sector was corrupted in all input files
            if bad_sector:
                # write corrupted sector data to output file
                fout.write(reverse_bits(corrected_data))

                # increment bad sector count
                bad_sector_count += 1

                # print("incorrectable:{} {} {}".format(block, page, sector))

            # process remaining sectors
            for sector in range(1, sectors_per_page):
                # initialize bad sector flag
                bad_sector = False

                # increment count of processed sectors
                processed_sector_count += 1

                # use all input files, if required (early exit condition)
                for mmin in input_file_mmaps:
                    # read page of current memory-map
                    start_page = start_block + page * config['fullpagesize']
                    end_page = start_page + config['fullpagesize']
                    page_data = mmin[start_page:end_page]

                    # get data of current sector
                    start_data = sector * config['sectorsize'] + \
                                 config['metadata_size'] + \
                                 sector * int(config['ecc_bytes_per_sector'])
                    end_data = start_data + config['sectorsize']
                    sector_data = reverse_bits(page_data[start_data:end_data])

                    # get ECC of current sector
                    start_ecc = start_data + config['sectorsize']
                    end_ecc = start_ecc + int(config['ecc_bytes_per_sector'])
                    sector_ecc = reverse_bits(page_data[start_ecc:end_ecc])

                    # # calculate ECC
                    # calc_ecc = bch.encode(sector_data)

                    # apply BCH error correcting code
                    corrected = bch.decode(sector_data, sector_ecc)

                    corrected_data = bytearray(sector_data)
                    corrected_ecc = bytearray(sector_ecc)
                    bch.correct(corrected_data, corrected_ecc)

                    if corrected >= 0 and corrected <= bch.t:
                        # correctable number of errors

                        if len(corrected_data) == config['sectorsize']:
                            # write corrected sector data to output file
                            fout.write(reverse_bits(corrected_data))

                            # increment good sector count
                            good_sector_count += 1

                            # clear bad sector flag
                            bad_sector = False

                            # sector had no bit errors
                            if corrected == 0:
                                uncorrected_sector_count += 1
                            else:
                                corrected_sector_count += 1

                            # early exit if we have a good sector
                            break

                        else:
                            print("[-] Error: Corrected data of sector {} has "
                                  "wrong size ({})"
                                  .format(sector, config['sectorsize']))
                            # set bad sector flag
                            bad_sector = True
                    else:
                        # set bad sector flag
                        bad_sector = True

                # check if the sector was corrupted in all input files
                if bad_sector:
                    # write corrupted sector data to output file
                    fout.write(reverse_bits(corrected_data))

                    # increment bad sector count
                    bad_sector_count += 1

                    # print("incorrectable:{} {} {}".format(block, page, sector))

        # show some statistics during processing all sectors
        progress = processed_sector_count / total_sectors * 100
        print("\r    Progress: {:.2f}% ({}/{} sectors)"
              .format(progress, processed_sector_count, total_sectors), end="")

    # close output file
    fout.close()

    # close memory-maps
    for mm in input_file_mmaps:
        mm.close()

    # close input files
    for f in input_file_handles:
        f.close()

    # show some statistics at the end
    good_sector_percentage = good_sector_count / total_sectors * 100
    bad_sector_percentage = bad_sector_count / total_sectors * 100
    corrected_sector_percentage = corrected_sector_count / total_sectors * 100
    blank_page_percentage = blank_page_count / total_page_count * 100
    blank_sector_count = blank_page_count * sectors_per_page
    blank_sector_percentage = blank_sector_count / total_sectors * 100
    good_data_sector_count = good_sector_count - blank_sector_count
    good_data_sector_percentage = good_data_sector_count / total_sectors * 100
    data_sector_count = good_data_sector_count + bad_sector_count
    data_sector_percentage = data_sector_count / total_sectors * 100

    print("\n[*] Completed error correcting process")
    print("    Successfully written {} bytes of data to output file '{}'"
          .format(config['sectorsize'] * total_sectors, outfile))
    print("    -----\n    Some statistics\n"
          "    Total pages:        {}\n"
          "    Blank pages:        {} ({:.2f}%)\n"
          "    Blank sectors:      {} ({:.2f}%)\n"
          "    Data sectors:       {} ({:.2f}%)\n"
          "    Total sectors:      {}\n"
          "    Valid sectors:      {} ({:.2f}%)\n"
          "    Valid data sectors: {} ({:.2f}%)\n"
          "    Corrupted sectors:  {} ({:.2f}%)\n"
          "    Corrected sectors:  {} ({:.2f}%)\n"
          "    Bad blocks:         {}"
          .format(total_page_count, blank_page_count, blank_page_percentage,
                  blank_sector_count, blank_sector_percentage,
                  data_sector_count, data_sector_percentage,
                  total_sectors, good_sector_count, good_sector_percentage,
                  good_data_sector_count, good_data_sector_percentage,
                  bad_sector_count, bad_sector_percentage,
                  corrected_sector_count, corrected_sector_percentage,
                  bad_block_count))

    return


def nxp_p1014_error_correction(infiles, outfile, config):
    """Do some error correction"""

    # initialize BCH decoder
    bch = bchlib.BCH(config['ecc_errors'], config['ecc_polynom'])

    # open output file
    fout = open(outfile, "wb")

    # initialize some variables
    processed_sector_count = 0
    corrected_sector_count = 0
    uncorrected_sector_count = 0
    good_sector_count = 0
    bad_sector_count = 0
    blank_page_count = 0
    bad_block_count = 0
    total_page_count = config['filesize'] // config['fullpagesize']
    sectors_per_page = config['pagesize'] // config['sectorsize']
    total_sectors = total_page_count * sectors_per_page
    page_size = config['pagesize']
    block_size_bytes = config['fullpagesize'] * config['blocksize']
    total_blocks = config['filesize'] // block_size_bytes

    # blank page data
    blank_page = b'\xff' * config['fullpagesize']

    # work with input files
    input_file_handles = []
    input_file_index = 0
    for f in infiles:
        input_file_handles.append(open(f, "r+b"))

    # memory-map the input files
    input_file_mmaps = []
    for fin in input_file_handles:
        input_file_mmaps.append(mmap.mmap(fin.fileno(), 0))

    # set current input file memory-map
    mm = input_file_mmaps[input_file_index]

    # with open(infile, "rb") as fin:
    print("[*] Starting error correcting process ...")

    for block in range(total_blocks):
        # read current block data
        # fin.seek(block * block_size_bytes)
        # block_data = fin.read(block_size_bytes)

        start_block = block * block_size_bytes
        end_block = start_block + block_size_bytes
        block_data = mm[start_block:end_block]

        # process pages in block
        for page in range(config['blocksize']):
            start_page = page * config['fullpagesize']
            end_page = start_page + config['fullpagesize']
            page_data = block_data[start_page:end_page]

            # check if page is blank
            if page_data == blank_page:
                # increment blank page counter
                blank_page_count += 1

                # increment good sector counter
                good_sector_count += sectors_per_page

                # increment count of processed sectors
                processed_sector_count += sectors_per_page

                # write blank page to output file
                fout.write(blank_page[:page_size])

                # show some statistics during processing all sectors
                progress = processed_sector_count / total_sectors * 100
                print("\r    Progress: {:.2f}% ({}/{} sectors)"
                      .format(progress, processed_sector_count,
                              total_sectors), end="")
                continue

            # process sectors in page
            for sector in range(sectors_per_page):
                #input()
                # initialize bad sector flag
                bad_sector = False

                # increment count of processed sectors
                processed_sector_count += 1

                # use all input files, if required (early exit condition)
                for mmin in input_file_mmaps:
                    # read page of current memory-map
                    start_page = start_block + page * config['fullpagesize']
                    end_page = start_page + config['fullpagesize']
                    page_data = mmin[start_page:end_page]

                    # get ECC of current sector
                    start_ecc = config['pagesize'] + config['ecc_offset'] + \
                        config['ecc_bytes_per_sector'] * sector
                    end_ecc = start_ecc + config['ecc_bytes_per_sector'] -1
                    sector_ecc = page_data[start_ecc:end_ecc]

                    # get data of current sector
                    start_data = sector * config['sectorsize']
                    end_data = start_data + config['sectorsize']
                    sector_data = page_data[start_data:end_data]

                    # calculate ECC
                    # calc_ecc = reverse_bits(bch.encode(sector_data))

                    # apply BCH error correcting code
                    corrected = bch.decode(sector_data, sector_ecc)

                    corrected_data = bytearray(sector_data)
                    corrected_ecc = bytearray(sector_ecc)
                    bch.correct(corrected_data, corrected_ecc)

                    if corrected >= 0 and corrected <= bch.t:
                        # correctable number of errors

                        if len(corrected_data) == config['sectorsize']:
                            # write corrected sector data to output file
                            fout.write(corrected_data)

                            # increment good sector count
                            good_sector_count += 1

                            # clear bad sector flag
                            bad_sector = False

                            # sector had no bit errors
                            if corrected == 0:
                                uncorrected_sector_count += 1
                            else:
                                corrected_sector_count += 1

                            # early exit if we have a good sector
                            break

                        else:
                            print("[-] Error: Corrected data of sector {} has "
                                  "wrong size ({})"
                                  .format(sector, config['sectorsize']))
                            # set bad sector flag
                            bad_sector = True
                    else:
                        # set bad sector flag
                        bad_sector = True

                # check if the sector was corrupted in all input files
                if bad_sector:
                    # write corrupted sector data to output file
                    fout.write(reverse_bits(corrected_data))

                    # increment bad sector count
                    bad_sector_count += 1

                    # print("incorrectable:{} {} {}".format(block, page, sector))

        # show some statistics during processing all sectors
        progress = processed_sector_count / total_sectors * 100
        print("\r    Progress: {:.2f}% ({}/{} sectors)"
              .format(progress, processed_sector_count, total_sectors), end="")

    # close output file
    fout.close()

    # close memory-maps
    for mm in input_file_mmaps:
        mm.close()

    # close input files
    for f in input_file_handles:
        f.close()

    # show some statistics at the end
    good_sector_percentage = good_sector_count / total_sectors * 100
    bad_sector_percentage = bad_sector_count / total_sectors * 100
    corrected_sector_percentage = corrected_sector_count / total_sectors * 100
    blank_page_percentage = blank_page_count / total_page_count * 100
    blank_sector_count = blank_page_count * sectors_per_page
    blank_sector_percentage = blank_sector_count / total_sectors * 100
    good_data_sector_count = good_sector_count - blank_sector_count
    good_data_sector_percentage = good_data_sector_count / total_sectors * 100
    data_sector_count = good_data_sector_count + bad_sector_count
    data_sector_percentage = data_sector_count / total_sectors * 100

    print("\n[*] Completed error correcting process")
    print("    Successfully written {} bytes of data to output file '{}'"
          .format(config['sectorsize'] * total_sectors, outfile))
    print("    -----\n    Some statistics\n"
          "    Total pages:        {}\n"
          "    Blank pages:        {} ({:.2f}%)\n"
          "    Blank sectors:      {} ({:.2f}%)\n"
          "    Data sectors:       {} ({:.2f}%)\n"
          "    Total sectors:      {}\n"
          "    Valid sectors:      {} ({:.2f}%)\n"
          "    Valid data sectors: {} ({:.2f}%)\n"
          "    Corrupted sectors:  {} ({:.2f}%)\n"
          "    Corrected sectors:  {} ({:.2f}%)"
          .format(total_page_count, blank_page_count, blank_page_percentage,
                  blank_sector_count, blank_sector_percentage,
                  data_sector_count, data_sector_percentage,
                  total_sectors, good_sector_count, good_sector_percentage,
                  good_data_sector_count, good_data_sector_percentage,
                  bad_sector_count, bad_sector_percentage,
                  corrected_sector_count, corrected_sector_percentage))


def yaffs_error_correction(infiles, outfile, config):
    """Do some error correction"""

    # open output file
    fout = open(outfile, "wb")

    # initialize some variables
    processed_sector_count = 0
    corrected_sector_count = 0
    uncorrected_sector_count = 0
    good_sector_count = 0
    bad_sector_count = 0
    blank_page_count = 0
    bad_block_count = 0
    total_page_count = config['filesize'] // config['fullpagesize']
    sectors_per_page = config['pagesize'] // config['sectorsize']
    total_sectors = total_page_count * sectors_per_page
    page_size = config['pagesize']
    block_size_bytes = config['fullpagesize'] * config['blocksize']
    total_blocks = config['filesize'] // block_size_bytes

    # blank page data
    blank_page = b'\xff' * config['fullpagesize']

    # bad block offsets (YAFFS2)
    # bb_offset1 = config['pagesize']

    # work with input files
    input_file_handles = []
    input_file_index = 0
    for f in infiles:
        input_file_handles.append(open(f, "r+b"))

    # memory-map the input files
    input_file_mmaps = []
    for fin in input_file_handles:
        input_file_mmaps.append(mmap.mmap(fin.fileno(), 0))

    # set current input file memory-map
    mm = input_file_mmaps[input_file_index]

    # with open(infile, "rb") as fin:
    print("[*] Starting error correcting process ...")

    for block in range(total_blocks):
        # read current block data
        # fin.seek(block * block_size_bytes)
        # block_data = fin.read(block_size_bytes)

        start_block = block * block_size_bytes
        end_block = start_block + block_size_bytes
        block_data = mm[start_block:end_block]

        # bad block logic for YAFFS2 has to be added

        # 1st page bad block marker
        # b1 = block_data[bb_offset1]

        # if b1 != 0xff:
            # increment bad block counter
            # bad_block_count += 1
            # continue

        # process pages in block
        for page in range(config['blocksize']):
            start_page = page * config['fullpagesize']
            end_page = start_page + config['fullpagesize']
            page_data = block_data[start_page:end_page]

            # extract ECCs for all sectors from spare area
            read_ecc = yaffs_extract_ecc(page_data[config['pagesize']:],
                                         config['ecc_bytes_per_sector'],
                                         sectors_per_page)

            # check if page is blank
            if page_data == blank_page or len(read_ecc) == 0:
                # increment blank page counter
                blank_page_count += 1

                # increment good sector counter
                good_sector_count += sectors_per_page

                # increment count of processed sectors
                processed_sector_count += sectors_per_page

                # write blank page to output file
                fout.write(blank_page[:page_size])

                # show some statistics during processing all sectors
                progress = processed_sector_count / total_sectors * 100
                print("\r    Progress: {:.2f}% ({}/{} sectors)"
                      .format(progress, processed_sector_count,
                              total_sectors), end="")
                continue

            # process sectors in page
            for sector in range(sectors_per_page):
                # initialize bad sector flag
                bad_sector = False

                # increment count of processed sectors
                processed_sector_count += 1

                # use all input files, if required (early exit condition)
                for mmin in input_file_mmaps:
                    # read page of current memory-map
                    start_page = start_block + page * config['fullpagesize']
                    end_page = start_page + config['fullpagesize']
                    page_data = mmin[start_page:end_page]

                    # get data of current sector
                    start_data = sector * config['sectorsize']
                    end_data = start_data + config['sectorsize']
                    sector_data = page_data[start_data:end_data]

                    # calculate ECC
                    test_ecc = yaffs_calc_ecc_256(sector_data)
                    corrected = yaffs_ecc_correct(sector_data, read_ecc[sector], test_ecc)

                    if corrected == 1:
                        # correctable single bit error
                        corrected_sector_count += 1

                        # write corrected sector data to output file
                        fout.write(bytes(sector_data))

                        # increment good sector count
                        good_sector_count += 1

                        # clear bad sector flag
                        bad_sector = False

                        # early exit if we have a good sector
                        break

                    elif corrected == 0:
                        # write corrected sector data to output file
                        fout.write(bytes(sector_data))

                        # increment good sector count
                        good_sector_count += 1

                        # clear bad sector flag
                        bad_sector = False

                        # early exit if we have a good sector
                        break

                    else:
                        # set bad sector flag
                        bad_sector = True

                        # increment uncorrected sector count
                        uncorrected_sector_count += 1

                # check if the sector was corrupted in all input files
                if bad_sector:
                    # write corrupted sector data to output file
                    fout.write(sector_data)

                    # increment bad sector count
                    bad_sector_count += 1

        # show some statistics during processing all sectors
        progress = processed_sector_count / total_sectors * 100
        print("\r    Progress: {:.2f}% ({}/{} sectors)"
              .format(progress, processed_sector_count, total_sectors), end="")

    # close output file
    fout.close()

    # close memory-maps
    for mm in input_file_mmaps:
        mm.close()

    # close input files
    for f in input_file_handles:
        f.close()

    # show some statistics at the end
    good_sector_percentage = good_sector_count / total_sectors * 100
    bad_sector_percentage = bad_sector_count / total_sectors * 100
    corrected_sector_percentage = corrected_sector_count / total_sectors * 100
    blank_page_percentage = blank_page_count / total_page_count * 100
    blank_sector_count = blank_page_count * sectors_per_page
    blank_sector_percentage = blank_sector_count / total_sectors * 100
    good_data_sector_count = good_sector_count - blank_sector_count
    good_data_sector_percentage = good_data_sector_count / total_sectors * 100
    data_sector_count = good_data_sector_count + bad_sector_count
    data_sector_percentage = data_sector_count / total_sectors * 100

    print("\n[*] Completed error correcting process")
    print("    Successfully written {} bytes of data to output file '{}'"
          .format(config['sectorsize'] * total_sectors, outfile))
    print("    -----\n    Some statistics\n"
          "    Total pages:        {}\n"
          "    Blank pages:        {} ({:.2f}%)\n"
          "    Blank sectors:      {} ({:.2f}%)\n"
          "    Data sectors:       {} ({:.2f}%)\n"
          "    Total sectors:      {}\n"
          "    Valid sectors:      {} ({:.2f}%)\n"
          "    Valid data sectors: {} ({:.2f}%)\n"
          "    Corrupted sectors:  {} ({:.2f}%)\n"
          "    Corrected sectors:  {} ({:.2f}%)\n"
          "    Bad blocks:         {}"
          .format(total_page_count, blank_page_count, blank_page_percentage,
                  blank_sector_count, blank_sector_percentage,
                  data_sector_count, data_sector_percentage,
                  total_sectors, good_sector_count, good_sector_percentage,
                  good_data_sector_count, good_data_sector_percentage,
                  bad_sector_count, bad_sector_percentage,
                  corrected_sector_count, corrected_sector_percentage,
                  bad_block_count))


def show_config(config):
    """Show configuration"""

    block_offset = config['file_offset'] // (config['blocksize'] *
                                             config['fullpagesize'])

    print("[*] Used configuration\n"
          "    Block size:  {} bytes ({} pages)\n"
          "    Page size:   {} bytes\n"
          "    Sector size: {} bytes\n"
          "    Spare size:  {} bytes\n"
          "    ECC offset:  {} bytes\n"
          "    ECC errors:  {} errors per sector (max.)\n"
          "    ECC bytes:   {} bytes per sector\n"
          "    Use ECC:     {}\n"
          "    File offset: 0x{:X} (skip {} blocks)"
          .format(config['blocksize'] * config['pagesize'],
                  config['blocksize'], config['pagesize'],
                  config['sectorsize'], config['spareareasize'],
                  config['ecc_offset'], config['ecc_errors'],
                  config['ecc_bytes_per_sector'], config['useecc'],
                  config['file_offset'], block_offset))


def banner():
    """Show a fancy banner"""

    print(
""" _   _   ___   _   _______  ______                        ______                   _           \n"""
"""| \ | | / _ \ | \ | |  _  \ |  _  \                       |  _  \                 | |          \n"""
"""|  \| |/ /_\ \|  \| | | | | | | | |_   _ _ __ ___  _ __   | | | |___  ___ ___   __| | ___ _ __ \n"""
"""| . ` ||  _  || . ` | | | | | | | | | | | '_ ` _ \| '_ \  | | | / _ \/ __/ _ \ / _` |/ _ \ '__|\n"""
"""| |\  || | | || |\  | |/ /  | |/ /| |_| | | | | | | |_) | | |/ /  __/ (_| (_) | (_| |  __/ |   \n"""
"""\_| \_/\_| |_/\_| \_/___/   |___/  \__,_|_| |_| |_| .__/  |___/ \___|\___\___/ \__,_|\___|_|   \n"""
"""                                                  | |                                          \n"""
"""                                                  |_|                                          \n"""
"""NAND Dump Decoder v{0} by Matthias Deeg - SySS GmbH (c) 2018-2020\n---""".format(__version__))


# supported vendor specific NAND layout with specific ECC algorithm
NAND_LAYOUT = {
            "ATMEL": atmel_error_correction,
            "ATMEL2": atmel2_error_correction,          # my additions
            "SOME": some_error_correction,          # my additions
            "WINBOND": winbond_error_correction,        # my additions
            "NXP_IMX28": nxp_imx28_error_correction,
            "NXP_P1014": nxp_p1014_error_correction,
            "YAFFS2": yaffs_error_correction            # experimental support
        }


# main program
if __name__ == '__main__':
    
    # show banner
    banner()

    # init argument parser
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--infolder', type=str, help='Input folder with binary dump files (.bin)', required=True)
    parser.add_argument('-o', '--outfile', type=str, help='Output dump file', required=True)
    parser.add_argument('-c', '--config', type=str, help='Configuration file')
    parser.add_argument('-m', '--mode', type=str, help='Vendor specific NAND mode (ATMEL, WINBOND, NXP_IMX28, NXP_P1014, YAFFS2 [experimental])')
    parser.add_argument('--atmel-config', action="store_true", help='Retrieve ATMEL config from first page of the dump file')
    parser.add_argument('--nxp-fcb-config', action="store_true", help='Retrieve NXP config from firmware control block (FCB) of first page of the dump file')

    # parse arguments
    args = parser.parse_args()

    # create empty configuration
    config = {}

    # check if input folder contains binary dump files (.bin)
    input_files = []
    for e in os.listdir(args.infolder):
        if str.lower(os.path.splitext(e)[1]) == DUMP_FILE_EXTENSION:
            input_files.append(os.path.normpath("{}/{}"
                               .format(args.infolder, e)))

    if len(input_files) == 0:
        print("[-] The input folder does not contain any binary dump files (.bin)")

    # only process process input files of the same size
    # the first binary file in the folder is our reference file
    infile_size = os.path.getsize(input_files[0])
    config['filesize'] = infile_size
    for f in input_files[1:]:
        file_size = os.path.getsize(f)

        if file_size != infile_size:
            # remove file from list if the size does not match
            input_files.remove(f)

    if len(input_files) == 1:
        print("[*] Found one binary input file ({} bytes)"
              .format(config['filesize']))
    else:
        print("[*] Found {} binary input files of the same size ({} bytes)"
              .format(len(input_files), config['filesize']))

    # check if ATMEL configuration within NAND dump should be used
    if args.atmel_config:
        # set some default ATMEL configuration parameters
        config['mode'] = "ATMEL"
        config['blocksize'] = 64
        # config['pagesize'] = 2048
        config['sectorsize'] = 512
        config['spareareasize'] = 64
        config['ecc_errors'] = 4
        config['ecc_polynom'] = ECC_POLY1
        config['file_offset'] = 0

        # try to identify page size (usually 2048 or 4096)
        with open(input_files[0], "rb") as f:
            data = f.read(2048 * 8)

            # set invalid page saze
            config['pagesize'] = 0

            # use a simple check for 0xff bytes in spare area to determine
            # the used page size
            for ps in [512, 1024, 2048, 4096, 8192]:
                if (data[ps] == 0xff) and (data[ps + 1] == 0xff):
                    config['pagesize'] = ps

        if config['pagesize'] == 0:
            print("[-] Error: Could not determine used page size.\n"
                  "Please use a correct config file for this NAND memory dump.")
            sys.exit(1)

        config['fullpagesize'] = config['pagesize'] + config['spareareasize']

        # read ATMEL PMECC configuration from first page of first input file
        with open(input_files[0], "rb") as f:
            first_page = f.read(config['fullpagesize'])
            config = read_atmel_config(first_page, config)
    elif args.nxp_fcb_config:
        # set some default NXP i.MX28 configuration parameters
        config['mode'] = "NXP_IMX28"
        config['file_offset'] = 0x2100000
        # config['file_offset'] = 0x108000
        config['ecc_polynom'] = ECC_POLY1

        # read NXP firmware control block (FCB) first page of first input file
        with open(input_files[0], "rb") as f:
            first_page = f.read(2048)
            config = read_nxp_imx28_config(first_page, config)

    else:
        if not args.config:
            print("[-] Error: need set Config file")
            sys.exit(1)
        # read configuration from given config file
        if not os.path.isfile(args.config):
            print("[-] Error: Config file '{}' does not exist".format(args.config))
            sys.exit(1)

        print("[*] Read configuration file '{}'".format(args.config))
        configfile = configparser.ConfigParser()
        try:
            configfile.read(args.config)

            # convert data types of parsed config data
            config['mode'] = configfile['default']['mode']
            config['blocksize'] = int(configfile['default']['blocksize'])
            config['pagesize'] = int(configfile['default']['pagesize'])
            config['sectorsize'] = int(configfile['default']['sectorsize'])
            config['spareareasize'] = int(configfile['default']['spareareasize'])
            config['useecc'] = bool(configfile['default']['useecc'])
            config['ecc_offset'] = int(configfile['default']['ecc_offset'])
            config['ecc_errors'] = int(configfile['default']['ecc_errors'])
            config['ecc_polynom'] = int(configfile['default']['ecc_polynom'], 16)
            config['ecc_errors'] = int(configfile['default']['ecc_errors'])
            config['ecc_bytes_per_sector'] = int(configfile['default']['ecc_bytes_per_sector'])
            config['file_offset'] = int(configfile['default']['file_offset'], 16)
            config['sectors_per_page'] = int(configfile['default']['sectorsperpage'])
            config['metadata_size'] = int(configfile['default']['metadatasize'])

        except KeyError:
            print("[-] Error: Could not read all required configuration values")
            sys.exit(1)

    # check ECC mode
    if config['mode'] in NAND_LAYOUT.keys():
        print("[*] Using ECC mode {}".format(config['mode']))
    else:
        print(args)
        if args.mode.upper() not in NAND_LAYOUT.keys():
            print("[-] Error: ECC mode is not set")
            sys.exit(1)

        # set ECC mode
        config['mode'] = args.mode.upper()

    # add derivated configuration parameters
    config['fullpagesize'] = config['pagesize'] + config['spareareasize']

    # show config
    show_config(config)

    if config['useecc']:
        ecc_method = NAND_LAYOUT[config['mode']]
        ecc_method(input_files, args.outfile, config)
