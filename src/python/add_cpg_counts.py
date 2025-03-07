#!/usr/bin/python3 -u

import os
import os.path as op
import subprocess
import shlex
import datetime
from multiprocessing import Pool
import argparse
from init_genome import chromosome_order
from utils_wgbs import IllegalArgumentError, match_maker_tool, eprint, \
        add_cpg_count_tool, validate_local_exe
from bam2pat import add_args, subprocess_wrap, validate_bam, is_pair_end, MAPQ, extend_region
from genomic_region import GenomicRegion


BAM_SUFF = '.bam'

# Minimal Mapping Quality to consider.
# 10 means include only reads w.p. >= 0.9 to be mapped correctly.
# And missing values (255)


def proc_chr(input_path, out_path_name, region, genome, header_path, paired_end, ex_flags, mapq, debug, min_cpg, clip,
             bed_file, add_pat, in_flags):
    """ Convert a temp single chromosome file, extracted from a bam file,
        into a sam formatted (no header) output file."""

    # Run patter tool 'bam' mode on a single chromosome

    unsorted_bam = out_path_name + '_unsorted.output.bam'
    out_path = out_path_name + '.output.bam'
    out_directory = os.path.dirname(out_path)

    # use samtools to extract only the reads from 'chrom'
    # flag = '-f 3' if paired_end else ''
    if in_flags is None:
        in_flags = '-f 3' if paired_end else ''
    else:
        in_flags = f'-f {in_flags}'
    cmd = "samtools view {} {} -q {} -F {} {}".format(input_path, region, mapq, ex_flags, in_flags)
    if bed_file is not None:
        cmd += f"-M -L {bed_file} | "
    else:
        cmd += "| "
    if debug:
        cmd += ' head -200 | '
    if paired_end:
        # change reads order, s.t paired reads will appear in adjacent lines
        cmd += f'{match_maker_tool} | '
    cmd += f'{add_cpg_count_tool} {genome.dict_path} {extend_region(region)} --clip {clip}'
    if min_cpg is not None:
        cmd += f' --min_cpg {str(min_cpg)}'
    if add_pat:
        cmd += ' --pat'
    cmd += f' | cat {header_path} - | samtools view -b - > {unsorted_bam}'

    sort_cmd = f'samtools sort -o {out_path} -T {out_directory} {unsorted_bam}'  # TODO: use temp directory, as in bam2pat

    # print(cmd)
    subprocess_wrap(cmd, debug)
    subprocess_wrap(sort_cmd, debug)
    os.remove(unsorted_bam)
    return out_path

def get_header_command(input_path):
    return f'samtools view -H {input_path}'

def proc_header(input_path, out_path, debug):
    """ extracts header from bam file and saves it to tmp file."""

    cmd = get_header_command(input_path) + f' > {out_path} '
    #print(cmd)
    subprocess_wrap(cmd, debug)

    return out_path


class BamMethylData:
    def __init__(self, args, bam_path):
        self.args = args
        self.out_dir = args.out_dir
        self.bam_path = bam_path
        self.debug = args.debug
        self.add_pat = args.add_pat
        self.gr = GenomicRegion(args)
        self.validate_input()

    def validate_input(self):

        # validate bam path:
        validate_bam(self.bam_path)

        # validate output dir:
        if not (op.isdir(self.out_dir)):
            raise IllegalArgumentError('Invalid output dir: {}'.format(self.out_dir))

    # def set_regions(self):
        # if self.gr.region_str:
            # return [self.gr.region_str]
        # else:
            # cmd = 'samtools idxstats {} | cut -f1 '.format(self.bam_path)
            # p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # output, error = p.communicate()
            # if p.returncode or not output:
                # print(cmd)
                # print("Failed with samtools idxstats %d\n%s\n%s" % (p.returncode, output.decode(), error.decode()))
                # print('falied to find chromosomes')
                # return []
            # nofilt_chroms = output.decode()[:-1].split('\n')
            # filt_chroms = [c for c in nofilt_chroms if 'chr' in c]
            # if not filt_chroms:
                # filt_chroms = [c for c in nofilt_chroms if c in CHROMS]
            # else:
                # filt_chroms = [c for c in filt_chroms if re.match(r'^chr([\d]+|[XYM])$', c)]
            # if not filt_chroms:
                # eprint('Failed retrieving valid chromosome names')
                # raise IllegalArgumentError('Failed')
            # return filt_chroms

    def set_regions(self):
        # if user specified a region, just use it
        if self.gr.region_str:
            return [self.gr.region_str]

        # get all chromosomes present in the bam file header
        cmd = f'samtools idxstats {self.bam_path} | cut -f1 '
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = p.communicate()
        if p.returncode or not output:
            eprint("[wt acc] Failed with samtools idxstats %d\n%s\n%s" % (p.returncode, output.decode(), error.decode()))
            eprint(cmd)
            eprint('[wt acc] falied to find chromosomes')
            return []
        bam_chroms = output.decode()[:-1].split('\n')

        # get all chromosomes from the reference genome:
        ref_chroms = self.gr.genome.get_chroms()
        # intersect the chromosomes from the bam and from the reference
        intersected_chroms = list(set(bam_chroms) & set(ref_chroms))

        if not intersected_chroms:
            msg = '[wt acc] Failed retrieving valid chromosome names. '
            msg += 'Perhaps you are using a wrong genome reference. '
            msg += 'Try running:\n\t\twgbstools set_default_ref -ls'
            msg += '\nMake sure the chromosomes in the bam header exists in the reference fasta'
            eprint(msg)
            raise IllegalArgumentError('Failed')

        return list(sorted(intersected_chroms, key=chromosome_order))  # todo use the same order as in ref_chroms instead of resorting it

    def intermediate_bam_file_view(self, name):
        return '<(samtools view {})'.format(name)

    def process_substitute(self, cmd):
        return '<({})'.format(cmd)

    def start_threads(self):
        """ Parse each chromosome file in a different process,
            and concatenate outputs to pat and unq files """
        print(datetime.datetime.now().isoformat() + ": *** starting processing of each chromosome")
        name = op.join(self.out_dir, op.basename(self.bam_path)[:-4])
        header_path = name + '.header'
        proc_header(self.bam_path, header_path, self.debug)
        if self.gr.region_str is None:
            final_path = name + f".{self.args.suffix}" + BAM_SUFF
            processes = []
            with Pool(self.args.threads) as p:
                for c in self.set_regions():
                    out_path_name = name + '_' + c
                    params = (self.bam_path, out_path_name, c, self.gr.genome,
                            header_path, is_pair_end(self.bam_path), self.args.exclude_flags,
                            self.args.mapq, self.debug, self.args.min_cpg, self.args.clip, self.args.regions_file,
                              self.add_pat, self.args.include_flags)
                    processes.append(p.apply_async(proc_chr, params))
                if not processes:
                    raise IllegalArgumentError('Empty bam file')
                p.close()
                p.join()
            res = [pr.get() for pr in processes]   # [(pat_path, unq_path) for each chromosome]
        else:
            region_str_for_name = self.gr.region_str.replace(":", "_").replace("-", "_")
            final_path = name + f".{region_str_for_name}" + f".{self.args.suffix}" + BAM_SUFF
            out_path_name = name + '_' + "1"
            res = [proc_chr(self.bam_path, out_path_name, self.gr.region_str, self.gr.genome, header_path,
                            is_pair_end(self.bam_path), self.args.exclude_flags, self.args.mapq, self.debug,
                            self.args.min_cpg, self.args.clip, self.args.regions_file,
                              self.add_pat, self.args.include_flags)]
        print('finished adding CpG counts')
        if None in res:
            print('threads failed')
            return

        print(datetime.datetime.now().isoformat() + ": finished processing each chromosome")
        # Concatenate chromosome files

        out_directory = os.path.dirname(final_path)
        # cmd = '/bin/bash -c "cat <({})'.format(get_header_command(self.bam_path)) + ' ' +\
        #       ' '.join([self.intermediate_bam_file_view(p) for p in res]) + ' | samtools view -b - > ' + final_path_unsorted + '"'
        cmd = f"samtools merge -c -p -f -h {header_path} {final_path} " + ' '.join([p for p in res])
        # cmd = '/bin/bash -c "samtools cat -h <({})'.format(get_header_command(self.bam_path)) + ' ' + \
        #       ' '.join(
        #           [p for p in res]) + ' > ' + final_path + '"'
        print(datetime.datetime.now().isoformat() + ': starting cat of files')
        process = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stdin=subprocess.PIPE)
        stdout, stderr = process.communicate()
        print(datetime.datetime.now().isoformat() + ": finished cat of files")

        # sort_cmd = 'samtools sort -o {} -T {} {}'.format(final_path, out_directory, final_path_unsorted)
        # print(datetime.datetime.now().isoformat() + ': starting sort of file')
        # sort_process = subprocess.Popen(shlex.split(sort_cmd), stdout=subprocess.PIPE, stdin=subprocess.PIPE)
        # stdout, stderr = sort_process.communicate()
        # print(datetime.datetime.now().isoformat() + ": finished sort of file")

        idx_command = f"samtools index {final_path}"
        print('starting index of output bam ' + datetime.datetime.now().isoformat())
        idx_process = subprocess.Popen(shlex.split(idx_command), stdout=subprocess.PIPE, stdin=subprocess.PIPE)
        stdout, stderr = idx_process.communicate()
        print(datetime.datetime.now().isoformat() + ": finished index of output bam")
        res.append(header_path)
        # remove all small files
        list(map(os.remove, [l for l in res]))


def add_cpg_args(parser):
    parser.add_argument('--regions_file',  default=None,
                        help='A bed file of genomic regions. These regions will be used to filter the input bam.')
    parser.add_argument('--suffix', default="counts",
                        help='The output file suffix. The output file will be [in_file].[suffix].bam. By default the '
                             'suffix is "counts".')
    parser.add_argument('--add_pat', action='store_true',
                        help='Indicates whether to add the methylation pattern of the read (pair).')
    return parser


def main():
    """
    Add to bam file an extra field, YI:Z:{nr_meth},{nr_unmeth},
    to count Cytosine retention at CpG context.
    """
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser = add_args(parser)
    parser = add_cpg_args(parser)
    args = parser.parse_args()
    validate_local_exe(add_cpg_count_tool)
    for bam in args.bam:
        if not validate_bam(bam):
            eprint(f'[wt add_cpg_counts] Skipping {bam}')
            continue
        BamMethylData(args, bam).start_threads()


if __name__ == '__main__':
    main()
