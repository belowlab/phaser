import multiprocessing;
import string;
import argparse;
import gzip;
import tempfile;
import subprocess;
import itertools;
from intervaltree import IntervalTree;
import sys;
import time;
from scipy.stats import binom;
import numpy;
import os;
import vcf;
import pysam;
import math;
import copy;

def main():
	#Arguments passed 
	parser = argparse.ArgumentParser()
	# required
	parser.add_argument("--bam", help="Indexed BAMs (comma separated) containing aligned reads", required = True)
	parser.add_argument("--vcf", help="VCF for sample, must be gzipped and tabix indexed.", required = True)
	parser.add_argument("--sample", help="Sample name in VCF", required = True)
	parser.add_argument("--mapq", help="Minimum MAPQ for reads to be used for phasing. Can be a comma separated list, each value corresponding to the min MAPQ for a file in the input BAM list. Useful in cases when using both for example DNA and RNA libraries which having different mapping qualities.", required = True)
	parser.add_argument("--baseq", type=int, help="Minimum baseq for bases to be used for phasing", required = True)
	parser.add_argument("--paired_end", type=int, help="Sequencing data comes from a paired end assay (0,1). If set to true phASER will require all reads to have the 'read mapped in proper pair' flag.", required = True)
	parser.add_argument("--o", help="Out prefix",required = True)
	
	# optional
	parser.add_argument("--python_string", default="python2.7", help="Command to use when calling python, required for running read variant mapping script.")
	parser.add_argument("--haplo_count_bam", default="", help="Comma separated list of BAMs to use when generating haplotypic counts (outputted in o.haplotypic_counts.txt). When left blank will use all libraries for counts, otherwise will only use the libraries specified here. Specify libraries by index where 1 = first library in --bam list, 2 = second, etc...")
	parser.add_argument("--haplo_count_blacklist", default="", help="BED file containing genomic intervals to be excluded from haplotypic counts. Reads from any variants which lie within these regions will not be counted for haplotypic counts.")
	parser.add_argument("--cc_threshold", type=float, default=0.01, help="Threshold for significant conflicting variant configuration. The connection between any two variants with a conflicting configuration p-value lower than this threshold will be removed.")
	parser.add_argument("--isize", default="0", help="Maximum allowed insert size for read pairs. Can be a comma separated list, each value corresponding to a max isize for a file in the input BAM list. Set to 0 for no maximum size.")
	parser.add_argument("--as_q_cutoff", type=float, default=0.05, help="Bottom quantile to cutoff for read alignment score.")
	parser.add_argument("--blacklist", default="", help="BED file containing genomic intervals to be excluded from phasing (for example HLA).")
	parser.add_argument("--write_vcf", type=int, default=1, help="Create a VCF containing phasing information (0,1).")
	parser.add_argument("--include_indels", type=int, default=0, help="Include indels in the analysis (0,1). NOTE: since mapping is a problem for indels including them will likely result in poor quality phasing unless specific precautions have been taken.")
	parser.add_argument("--output_read_ids", type=int, default=0, help="Output read IDs in the coverage files (0,1).")
	parser.add_argument("--remove_dups", type=int, default=1, help="Remove duplicate reads from all analyses (0,1).")
	parser.add_argument("--pass_only", type=int, default=1, help="Only use variants labled with PASS in the VCF filter field (0,1).")
	parser.add_argument("--min_cov", type=int, default=0, help="Minimum total coverage level before outputting haplotypic counts.")
	parser.add_argument("--unphased_vars", type=int, default=1, help="Output unphased variants (singletons) in the haplotypic_counts and haplotypes files (0,1).")
	parser.add_argument("--chr_prefix", type=str, default="", help="Add the string to the begining of the VCF contig name. For example set to 'chr' if VCF contig is listed as '1' and bam reference is 'chr1'.")
	
	# genome wide phasing
	parser.add_argument("--gw_phase_method", type=int, default=0, help="Method to use for determing genome wide phasing. NOTE requires input VCF to be phased, and optionally a VCF with allele frequencies (see --gw_af_vcf). 0 = Use most common haplotype phase. 1 = MAF weighted phase anchoring.")
	parser.add_argument("--gw_af_vcf", default="", help="VCF with allele frequencies from the population which was used to do the phasing. If left blank it will look for an allele frequency in the input VCF (--vcf).")
	parser.add_argument("--gw_af_field", default="AF", help="Field from --gw_af_vcf to use for allele frequency.")
	parser.add_argument("--gw_phase_vcf", type=int, default=0, help="Replace GT field of output VCF using phASER genome wide phase. 0: do not replace; 1: replace when gw_confidence >= --gw_phase_vcf_min_confidence; 2: as in (1), but in addition replace with haplotype block phase when gw_confidence < --gw_phase_vcf_min_confidence and include PS field. See --gw_phase_method for options.")
	parser.add_argument("--gw_phase_vcf_min_confidence", type=float, default=0.90, help="If replacing GT field in VCF only replace when phASER haplotype gw_confidence >= this value.")
	
	# performance
	parser.add_argument("--threads", type=int, default=1, help="Maximum number of threads to use. Note the maximum thread count for some tasks is bounded by the data (for example 1 thread per contig for haplotype construction).")
	parser.add_argument("--max_block_size", type=int, default=15, help="Maximum number of variants to phase at once. Number of haplotypes tested = 2 ^ # variants in block. Blocks larger than this will be split into sub blocks, phased, and then the best scoring sub blocks will be phased with each other.")
	parser.add_argument("--temp_dir", default="", help="Location of temporary directory to use for storing files. If left blank will default to system temp dir. NOTE: potentially large files will be stored in this directory, so please ensure there is sufficient free space.")
	parser.add_argument("--max_items_per_thread", type=int, default=100000, help="Maximum number of items that can be assigned to a single thread to process. NOTE: if this number is too high Python will stall when trying to join the pools.")
	
	# debug / development / reporting
	parser.add_argument("--show_warning", type=int, default=0, help="Show warnings in stdout (0,1).")
	parser.add_argument("--debug", type=int, default=0, help="Show debug mode messages (0,1).")
	parser.add_argument("--chr", default="", help="Restrict haplotype phasing to a specific chromosome.")
	parser.add_argument("--unique_ids", type=int, default=0, help="Generate and output unique IDs instead of those provided in the VCF (0,1). NOTE: this should be used if your VCF does not contain a unique ID for each variant.")
	parser.add_argument("--id_separator", default="_", help="Separator to use when generating unique IDs. Must not be found in contig name, and cannot include ':'.")
	parser.add_argument("--output_network", default="", help="Output the haplotype connection network for the given variant.")
	
	global args;
	args = parser.parse_args()
	
	#setup
	version = "0.9.6";
	fun_flush_print("");
	fun_flush_print("##################################################")
	fun_flush_print("              Welcome to phASER v%s"%(version));
	fun_flush_print("  Author: Stephane Castel (scastel@nygenome.org)")
	fun_flush_print("##################################################");
	fun_flush_print("");
	
	if args.id_separator == ":" or args.id_separator == "": fatal_error("ID separator must not be ':' or blank. Please choose another separator that is not found in the contig names.");
	contig_ban = [args.id_separator, ":"];
	
	if args.temp_dir != "":
		tempfile.tempdir = args.temp_dir;
	
	global devnull;
	devnull = open(os.devnull, 'w')
	
	# check for needed files
	needed_files = ['call_read_variant_map.py','read_variant_map.py'];
	for xfile in needed_files:
		if os.path.isfile(return_script_path()+"/"+xfile) == False:
			fatal_error("File %s is needed for phASER to run."%xfile);
	
	# check that setup has been run
	if os.path.isfile(return_script_path()+"/"+'read_variant_map.so') == False:
		fatal_error("Read Variant Mapper module must be compiled by running 'python setup.py build_ext --inplace'.");
	
	# check that all passed files actually exist
	if os.path.isfile(args.vcf) == False:
		fatal_error("VCF file does not exist.");
	elif os.path.isfile(args.vcf+".tbi") == False:
		fatal_error("VCF file is not tabix indexed.");
	if args.vcf.endswith(".gz") == False and args.vcf.endswith(".bgz") == False:
		fatal_error("VCF must be gzipped.");
		
	check_files = args.bam.split(",");
	for xfile in check_files:
		if xfile != "":
			if os.path.isfile(xfile) == False:
				fatal_error("File: %s not found."%(xfile));
			if os.path.isfile(xfile+".bai") == False and os.path.isfile(xfile.replace(".bam",".bai")) == False:
				fatal_error("Index for BAM %s not found. BAM files must be indexed, with naming 'sample.bam.bai'."%(xfile));
	
	check_files = [args.vcf,args.blacklist,args.haplo_count_blacklist];
	
	for xfile in check_files:
		if xfile != "":
			if os.path.isfile(xfile) == False:
				fatal_error("File: %s not found."%(xfile));
	
	global haplo_count_bam_indices;
	
	if args.haplo_count_bam != "":
		# split and subtract 1 to make 0 based index
		haplo_count_bam_indices = [x-1 for x in map(int, args.haplo_count_bam.split(","))];
	else:
		haplo_count_bam_indices = [];
	
	start_time = time.time();
	
	dict_haplo_blacklist_interval = {};
	
	if len(args.haplo_count_blacklist) > 0:
		fun_flush_print("#0. Loading haplotypic count blacklist intervals...");
		stream_in = open(args.haplo_count_blacklist,"r");
		for line in stream_in:
			columns = line.replace("\n","").split("\t");
			chr = columns[0];
			if chr not in dict_haplo_blacklist_interval: dict_haplo_blacklist_interval[chr] = IntervalTree();
			
			name = "";
			if len(columns) > 3: name = columns[3];
				
			dict_haplo_blacklist_interval[chr][int(columns[1]):int(columns[2])] = name;
	
		stream_in.close();
	
	# load the allele frequency VCF if specified
	if args.gw_af_vcf != "":
		if os.path.isfile(args.gw_af_vcf) == True:
			vcf_af = vcf.Reader(filename=args.gw_af_vcf);
		else:
			fatal_error("Allele frequency VCF (--gw_af_vcf) specified does not exist.");
		
	
	fun_flush_print("#1. Loading heterozygous variants into intervals...");
	
	# check to ensure sample is found in VCF
	
	map_sample_column = sample_column_map(args.vcf);
	
	global sample_column;
	
	if args.sample in map_sample_column:
		sample_column = map_sample_column[args.sample];
	else:
		fatal_error("Sample not found in VCF.");
	
	# filter blacklisted variants if necessary, cut only sample column, filter for heterozygous sites
	# decompress for intersection
	
	if args.chr != "":
		decomp_str = "tabix -h "+args.vcf+" "+args.chr+":"
	else:
		decomp_str = "gunzip -c "+args.vcf;
	
	vcf_out = tempfile.NamedTemporaryFile(delete=False);
	vcf_out.close();
	vcf_path = vcf_out.name;
		
	if args.blacklist != "":
		fun_flush_print("     removing blacklisted variants and processing VCF...");
		call_str = decomp_str + " | cut -f 1-9,"+str(sample_column+1)+" | grep -v '0|0\|1|1' | bedtools intersect -v -a stdin -b "+args.blacklist+" > "+vcf_out.name;
		error_code = subprocess.call(call_str,shell=True,stderr=devnull);
	else:
		fun_flush_print("     processing VCF...");
		call_str = decomp_str + " | cut -f 1-9,"+str(sample_column+1)+" | grep -v '0|0\|1|1' > "+vcf_out.name;
		error_code = subprocess.call(call_str,shell=True);
	
	if error_code != 0:
		fatal_error("VCF filtering using subprocess.call \""+call_str+"\" exited with an error")
		
	## PARSE the VCF, put it into a format that can be used by the mapper
	## one thread per chromosome
	
	stream_vcf = open(vcf_path, "r");
	mapper_out = tempfile.NamedTemporaryFile(delete=False);
	bed_out = tempfile.NamedTemporaryFile(delete=False);
	het_count = 0;
	total_indels_excluded = 0;
	unphased_count = 0;
	
	fun_flush_print("     creating variant mapping table...");
	if args.chr != "":
		fun_flush_print("          restricting to chromosome '%s'..."%(args.chr));
	
	gt_index = -1;
	
	chromosome_pool = {};
	filter_count = 0;
	
	for line in stream_vcf:
		vcf_columns = line.rstrip().split("\t");
		if line.startswith("#") == False:
			#1       10177   .       A       AC      100     PASS    AC=2130;AF=0.425319;AN=5008;NS=2504;DP=103152;EAS_AF=0.3363;AMR_AF=0.3602;AFR_AF=0.4909;EUR_AF=0.4056;SAS_AF=0.4949;AA=|||unknown(NO_COVERAGE)  GT      1|0
			unphased = False;
			chr = vcf_columns[0];
			for item in contig_ban:
				if item in chr: fatal_error("Character '%s' must not be present in contig name. Please change id separtor using --id_separator to a character not found in the contig names and try again."%(item));
			filter = vcf_columns[6];
			if args.chr == "" or args.chr == chr:
				if chr not in chromosome_pool: chromosome_pool[chr] = [];
				fields = vcf_columns[8].split(":");
				if "GT" in fields:
					if "GT" in fields:
						gt_index = fields.index("GT");
						geno_string = vcf_columns[9].split(":")[gt_index];
						xgeno = list(geno_string);
						if "." not in xgeno:
							if "|" in xgeno: xgeno.remove("|");
							if "/" in xgeno:
								xgeno.remove("/");
								unphased = True;
			
							if len(set(xgeno)) > 1:
								filters = filter.split(";");
								if args.pass_only == 0 or "PASS" in filters:
									chromosome_pool[chr].append(vcf_columns[0:9]+[geno_string,xgeno]);
									if unphased == True:
										unphased_count += 1;
								else:
									filter_count += 1;
					else:
						print_warning("Genotype, defined by GT not found in input VCF for variant %s."%(vcf_columns[2]));
						
	stream_vcf.close();
	bed_out.close();
	mapper_out.close();
	
	pool_input = [];
	for chrom in chromosome_pool.keys():
		pool_input.append([chrom,chromosome_pool[chrom]]);
	
	pool_output = parallelize(generate_mapping_table, pool_input);
	
	# clear memory
	del pool_input;
	del chromosome_pool;
	
	mapping_files = [];
	
	het_count = 0;
	total_indels_excluded = 0;
	for output in pool_output:
		mapping_files.append([output[0],output[3],output[4]]);
		het_count += output[1];
		total_indels_excluded += output[2];
		
	fun_flush_print("          %d heterozygous sites being used for phasing (%d filtered, %d indels excluded, %d unphased)"%(het_count,filter_count,total_indels_excluded,unphased_count));
	
	if het_count == 0:
		fatal_error("No heterozygous sites that passed all filters were included in the analysis, phASER cannot continue. Check blacklist and pass_only arguments.");
	
	fun_flush_print("#2. Retrieving reads that overlap heterozygous sites...");
	
	#works with multiple input bams
	bam_list = args.bam.split(",");
	
	#mapq
	mapq_list = args.mapq.split(",");
	if len(mapq_list) == 1 and len(bam_list) > 1:
		mapq_list = mapq_list * len(bam_list);
	elif len(mapq_list) != len(bam_list):
		fatal_error("Number of mapq values and input BAMs does not match. Supply either one mapq to be used for all BAMs or one mapq per input BAM.");
	
	#isize
	isize_list = args.isize.split(",");
	if len(isize_list) == 1 and len(bam_list) > 1:
		isize_list = isize_list * len(bam_list);
	elif len(mapq_list) != len(isize_list):
		fatal_error("Number of isize values and input BAMs does not match. Supply either one isize to be used for all BAMs or one isize per input BAM.");
	isize_list = map(float, isize_list);
	
	#now get bam reads that overlap het sites using SAMTOOLS
	samtools_arg = "";
	# remove dups if necessary, and only include properly paired read (ie in correct orientation)
	if args.remove_dups == 1:
		samtools_arg += "-F 0x400 "
	if args.paired_end == 1:
		samtools_arg += "-f 2"
	
	global dict_variant_reads;
	dict_variant_reads = {};
	
	global read_vars;
	read_vars = {};
	
	global bam_index;
	bam_index = 0;
	
	total_reads = 0;
	
	for bam, mapq, isize in zip(bam_list, mapq_list, isize_list):
		fun_flush_print("     file: %s"%(bam));
		fun_flush_print("          minimum mapq: %s"%(mapq));
		
		# use the read variant mapping script to map reads to alleles
		fun_flush_print("          mapping reads to variants...");
		pool_input = [x + [samtools_arg,bam,mapq,isize] for x in mapping_files];
		result_files = parallelize(call_mapping_script, pool_input);

		# process the result
		
		# A determine if we need to calculate alignment score cutoff
		fun_flush_print("          processing mapped reads...");
		
		global use_as_cutoff;
		global as_cutoff;
	
		use_as_cutoff = False;
		
		if args.as_q_cutoff > 0:
			alignment_scores = map(int,[x for x in subprocess.check_output("cut -f 5 "+" ".join(result_files), shell=True).split("\n") if x != ""]);
			if len(alignment_scores) == 0:
				fun_flush_print("          no alignment score value found in reads, cannot use cutoff");
			else:
				as_cutoff = numpy.percentile(alignment_scores,args.as_q_cutoff*100);
				use_as_cutoff = True;
				fun_flush_print("          using alignment score cutoff of %d"%(as_cutoff));
		
		# B now process variant read overlaps
		pool_output = parallelize(process_mapping_result, result_files);
		for output in pool_output:
			for variant in output[0]:
				if variant not in dict_variant_reads:
					dict_variant_reads[variant] = output[0][variant];
				else:
					dict_variant_reads[variant]['reads'][0] += output[0][variant]['reads'][0];
					dict_variant_reads[variant]['reads'][1] += output[0][variant]['reads'][1];
					dict_variant_reads[variant]['haplo_reads'][0] += output[0][variant]['haplo_reads'][0];
					dict_variant_reads[variant]['haplo_reads'][1] += output[0][variant]['haplo_reads'][1];
					dict_variant_reads[variant]['other_reads'] += output[0][variant]['other_reads'];
			
		for output in pool_output:
			if output[3] not in read_vars: read_vars[output[3]] = {};
			
		for output in pool_output:
			for read in output[1]:
				if variant not in read_vars[output[3]]:
					read_vars[output[3]][read] = output[1][read];
				else:
					read_vars[output[3]][read]  += output[1][read];
		
		bam_reads = 0;
		for output in pool_output:
			total_reads += output[2];
			bam_reads += output[2];
		
		del pool_output;
		
		fun_flush_print("          retrieved %d reads"%(bam_reads));
		bam_index += 1;
		
	#cleanup temp files
	os.remove(mapper_out.name);
	os.remove(bed_out.name);
	
	for xfile in result_files:
		os.remove(xfile);
	
	os.remove(vcf_out.name);
	fun_flush_print("#3. Identifying connected variants...");
	fun_flush_print("     calculating sequencing noise level...");
	
	# calculate noise level
	base_match_count = 0;
	base_mismatch_count = 0;
	
	for variant in dict_variant_reads:
		mis_matches = 0;
		
		mis_matches = len(dict_variant_reads[variant]['other_reads']);
		matches = sum([len(x) for x in dict_variant_reads[variant]['reads']]);
			
		# require other bases to be < 5% of total coverage for this variant
		# protects against genotyping errors
		if matches > 0 and (float(mis_matches) / float(mis_matches+matches)) < 0.50:
			base_match_count += matches;
			base_mismatch_count += mis_matches;
	
	if base_match_count == 0:
		fatal_error("No reads could be matched to variants. Please double check your settings and input files. Common reasons for this occurring include: 1) MAPQ or BASEQ set too conservatively 2) BAM and VCF have different chromosome names (IE 'chr1' vs '1').");
	
	# probability of generating a random base
	global noise_e;
	noise_e = (float(base_mismatch_count) / (float(base_match_count+base_mismatch_count)*2));
	fun_flush_print("     sequencing noise level estimated at %f"%(noise_e));
	
	# premake read sets for faster comparison
	fun_flush_print("     creating read sets...");
	for var_id in dict_variant_reads:
		dict_variant_reads[var_id]['read_set'] = [];
		for allele_reads in dict_variant_reads[var_id]['reads']:
			dict_variant_reads[var_id]['read_set'].append(set(allele_reads));
		dict_variant_reads[var_id]['other_read_set'] = set(dict_variant_reads[var_id]['other_reads']);

	# now create the quick lookup dictionary
	# this is used for haplotype construction
	# dictionary tells you what variants are connected
	fun_flush_print("     generating read connectivity map...");
	global dict_variant_overlap;
	
	dict_variant_overlap = {};
	
	pool_input = read_vars.keys();
	pool_output = parallelize(generate_connectivity_map, pool_input);
	
	for output in pool_output:
		dict_variant_overlap.update(output);
	
	# clear memory
	del pool_output;
	del read_vars;
	
	# make sets of overlaps
	for chr in dict_variant_overlap:
		for variant in dict_variant_overlap[chr]:
			dict_variant_overlap[chr][variant] = set(dict_variant_overlap[chr][variant]);
	
	## now run the test to determine if the number of reads with conflicting connections is
	## higher than noise for a given variant pair.
	## if so these two variants will be disconnected, so that they won't be used for haplotype construction
	tested_connections = set([]);
	pool_input = [];
	fun_flush_print("     testing variant connections versus noise...");
	for chr in dict_variant_overlap:
		for variant_a in dict_variant_overlap[chr]:
			overlapping_variants = dict_variant_overlap[chr][variant_a];
			for variant_b in overlapping_variants:
				key1 = variant_a+"|"+variant_b;
				key2 = variant_b+"|"+variant_a;
				if key1 not in tested_connections and key2 not in tested_connections:
					pool_input.append([chr,variant_a,variant_b]);
					tested_connections.add(key1);
	
	pool_output = parallelize(test_variant_connection, pool_input);
	
	out_stream = open(args.o+".variant_connections.txt","w");
	out_stream.write("variant_a\tvariant_b\tsupporting_connections\ttotal_connections\tconflicting_configuration_p\tphase_concordant\n");
	
	dict_allele_connections = {};
	
	# remove all those connections which failed
	c_dropped = 0;
	for connection in pool_output:
		chr,variant_a,variant_b,conflicting_config_p,c_supporting,c_total,phase_concordant,chosen_config = connection;
		
		# if the number of conflicting reads is more than would be expected from noise, then disconnect these two variants
		# they will not be used for haplotype construction
		out_stream.write("\t".join(map(str,[variant_a,variant_b,c_supporting,c_total,conflicting_config_p,phase_concordant]))+"\n");
		if conflicting_config_p < args.cc_threshold:
			#print("%s	%s"%(variant_a,variant_b));
			dict_variant_overlap[chr][variant_a].remove(variant_b);
			dict_variant_overlap[chr][variant_b].remove(variant_a);
		
			# if these variants have no other connections remove them from overlap dictionary
			if len(dict_variant_overlap[chr][variant_a]) == 0:
				del dict_variant_overlap[chr][variant_a];
			if len(dict_variant_overlap[chr][variant_b]) == 0:
				del dict_variant_overlap[chr][variant_b];
		
			c_dropped += 1;
		else:
			# didn't drop record the specific allele connections
			if variant_a+":0" not in dict_allele_connections: dict_allele_connections[variant_a+":0"] = set([]);
			if variant_a+":1" not in dict_allele_connections: dict_allele_connections[variant_a+":1"] = set([]);
			if variant_b+":0" not in dict_allele_connections: dict_allele_connections[variant_b+":0"] = set([]);
			if variant_b+":1" not in dict_allele_connections: dict_allele_connections[variant_b+":1"] = set([]);
			
			if chosen_config == 0:
				# 0 - 0 / 1 - 1
				dict_allele_connections[variant_a+":0"].add(variant_b+":0");
				dict_allele_connections[variant_b+":0"].add(variant_a+":0");
				dict_allele_connections[variant_a+":1"].add(variant_b+":1");
				dict_allele_connections[variant_b+":1"].add(variant_a+":1");
			elif chosen_config == 1:
				# 0 - 1 / 1 - 0
				dict_allele_connections[variant_a+":0"].add(variant_b+":1");
				dict_allele_connections[variant_b+":0"].add(variant_a+":1");
				dict_allele_connections[variant_a+":1"].add(variant_b+":0");
				dict_allele_connections[variant_b+":1"].add(variant_a+":0");
				

	out_stream.close();

	fun_flush_print("     %d variant connections dropped because of conflicting configurations (threshold = %f)"%(c_dropped,args.cc_threshold));
	
	# output the coverage level per snp
	# same format as GATK tool:
	stream_out = open(args.o+".allelic_counts.txt","w");
	stream_out.write("contig	position	variantID	refAllele	altAllele	refCount	altCount	totalCount\n");
	covered_count = 0;
	
	for variant in dict_variant_reads:
		snp_dict = dict_variant_reads[variant];
		
		ref_reads = len(set(snp_dict['reads'][0]));
		alt_reads = len(set(snp_dict['reads'][1]));
		if ref_reads+alt_reads > 0:
			covered_count += 1;
			stream_out.write("\t".join([snp_dict['chr'],str(snp_dict['pos']),variant,snp_dict['alleles'][0],snp_dict['alleles'][1],str(ref_reads),str(alt_reads),str(ref_reads+alt_reads)+"\n"]));
	stream_out.close();
	
	fun_flush_print("     %d variants covered by at least 1 read"%(covered_count));
	
	# record the total number of hets
	total_het_variants = len(dict_variant_reads.keys());
	
	remove_keys = [];
	if args.unphased_vars == 0:
		# clear all SNPs with no connections to others from the dictionary to free up memory
		# if we don;t want to output unphased snps
		
		for variant in dict_variant_reads:
			chr = dict_variant_reads[variant]['chr'];
			if chr not in dict_variant_overlap:
				remove_keys.append(variant);
			elif variant not in dict_variant_overlap[chr]:
				remove_keys.append(variant);
	else:
		# otherwise just remove variants with 0 coverage
		for variant in dict_variant_reads:
			if len(dict_variant_reads[variant]['reads'][0]) + len(dict_variant_reads[variant]['reads'][1]) == 0:
				remove_keys.append(variant);
	
	for key in remove_keys:
		del dict_variant_reads[key];
		
	print_debug("     removed %d variants from memory in cleanup"%(len(remove_keys)));
	
	# using only the overlapping SNP dictionary build haplotype blocks
	fun_flush_print("#4. Identifying haplotype blocks...");
	
	block_haplotypes = [];
	phased_vars = 0;
	
	pool_output = parallelize(build_haplotypes, dict_variant_overlap.values());
	
	for chr_haplotypes in pool_output:
		for haplotype_block in chr_haplotypes:
			block_haplotypes.append(haplotype_block);
			phased_vars += len(haplotype_block);
	
	# now for each of the blocks identify the phasing with the most supporting reads
	fun_flush_print("#5. Phasing blocks...");
	pool_input = [];

	for block in block_haplotypes:
		# retrieve all allele connections for block;
		variant_connections = {};
		allele_connections = {};
		for variant in block:
			chr = variant.split(args.id_separator)[0];
			if variant in dict_variant_overlap[chr]: variant_connections[variant] = dict_variant_overlap[chr][variant];
			if variant+":0" in dict_allele_connections: allele_connections[variant+":0"] = dict_allele_connections[variant+":0"]
			if variant+":1" in dict_allele_connections: allele_connections[variant+":1"] = dict_allele_connections[variant+":1"]

		pool_input.append([sort_var_ids(block),variant_connections,allele_connections]);
	
	pool_output = parallelize(phase_v3, pool_input);
	final_haplotypes = [];
	for output in pool_output:
		for block in output:
			if block != []:
				final_haplotypes.append(block);
	#print(final_haplotypes);
	del pool_output;
	del pool_input;
	
	#pool_input = pool_split(args.threads, pool_input);
	#pool_output = parallelize(phase_block_container, pool_input);
	
	#final_haplotypes = [];
	#for blocks in pool_output:
	#	for block in blocks:
	#		for sub_block in block:
	#			if len(sub_block) > 1:
	#				final_haplotypes.append(sub_block);
	
	#del pool_output;
	#del pool_input;
	
	fun_flush_print("#6. Outputting haplotypes...");	
	
	stream_out_ase = open(args.o+".haplotypic_counts.txt","w");
	ase_columns = ["contig","start","stop","variants","variantCount","haplotypeA","haplotypeB","aCount","bCount","totalCount","blockGWPhase","gwStat"];
	if args.output_read_ids == 1:
		ase_columns += ["read_ids_a","read_ids_b"];
	stream_out_ase.write("\t".join(ase_columns)+"\n");
	
	stream_out = open(args.o+".haplotypes.txt","w");
	stream_out.write("\t".join(['contig','start','stop','length','variants','variant_ids','variant_alleles','reads_hap_a','reads_hap_b','reads_total','edges_supporting','edges_total','annotated_phase','phase_concordant','gw_phase','gw_confidence'])+"\n");
	
	stream_out_allele_configs = open(args.o+".allele_config.txt","w");
	stream_out_allele_configs.write("\t".join(['variant_a','rsid_a','variant_b','rsid_b','configuration'])+"\n");
		
	global haplotype_lookup;
	haplotype_lookup = {};
	global haplotype_pvalue_lookup;
	haplotype_pvalue_lookup = {};
	global haplotype_gw_stat_lookup;
	haplotype_gw_stat_lookup = {};
	
	all_variants = [];
	
	block_index = 0;
	
	
	for block in final_haplotypes:
		#get all unique variants
		block_index += 1;
		variants = [x.split(":")[0] for x in block];
		variants = sort_var_ids(variants);
		
		all_variants += variants;
		
		haplotype_a = "".join([x.split(":")[1] for x in block]);
		haplotype_b = "".join([str(int(not int(x))) for x in haplotype_a]);
		
		# determine number of supporting edges vs total edges for this haplotype
		supporting_connections = 0;
		total_connections = 0;
		
		for allele in block:
			variant = allele.split(":")[0];
			for other_allele in block:
				if allele != other_allele:
					other_variant = other_allele.split(":")[0];
					# check if the configuration supports the phasing
					if other_allele in dict_allele_connections[allele]:
						supporting_connections += 1;
					
					if other_variant+":0" in dict_allele_connections[allele]:
						total_connections += 1;
					if other_variant+":1" in dict_allele_connections[allele]:
						total_connections += 1;
		
		supporting_connections = supporting_connections / 2;
		total_connections = total_connections / 2;
		
		if args.unique_ids == 0:
			rsids = [dict_variant_reads[x]['rsid'] for x in variants];
		else:
			rsids = variants;
				
		chrs = [dict_variant_reads[x]['chr'] for x in variants];
		positions = map(int, [dict_variant_reads[x]['pos'] for x in variants]);
		
		hap_p = 0;
		haplotype_pvalue_lookup[list_to_string(variants)] = hap_p;
		
		for var_index in range(0, len(variants)):	
			id = variants[var_index];
			haplotype_lookup[id] = [variants, haplotype_a[var_index]+"|"+haplotype_b[var_index],block_index];
			
		alleles = [[],[]];
		phases = [[],[]];
		set_reads = [[],[]];
		hap_counts = [0,0];
		
		for hap_index in range(0,2):
			hap_x = [haplotype_a, haplotype_b][hap_index];
			
			for var_index in range(0, len(variants)):
				id = variants[var_index];
				allele = dict_variant_reads[id]['alleles'][int(hap_x[var_index])];
				alleles[hap_index].append(allele);
				phases[hap_index].append(get_allele_phase(allele,dict_variant_reads[id]));
				
				allele_index = dict_variant_reads[id]['alleles'].index(allele);
				
				set_reads[hap_index] += dict_variant_reads[id]['reads'][allele_index];
				
			set_reads[hap_index] = list(set(set_reads[hap_index]));
			hap_counts[hap_index] = len(set_reads[hap_index]);
			
		# determine if phasing is completely concordant
		# don't include variants whose phase was unknown in the original VCF
		use_phases = [x for x in phases[0] if str(x) != "nan"];
		if len(set(use_phases)) <= 1:
			phase_concordant = 1;
		else:
			phase_concordant = 0;
		
		phase_string = ["",""]
		phase_string[0] = "".join([str(x).replace("nan", "-") for x in phases[0]]);
		phase_string[1] = "".join([str(x).replace("nan", "-") for x in phases[1]]);
		
		### GENOME WIDE PHASING
		# how many population phased variants do we have in this hap
		nan_strip = [int(x) for x in phases[0] if x >= 0];
		
		# by default corrected is the same as population
		corrected_phases = [phases[0],phases[1]];
		cor_phase_stat = 0.5;
		
		if len(nan_strip) > 0:
			# if setting is on determine genome wide phasing
			if args.gw_phase_method == 0:
				# phase using most common phase
				cor_phase_stat = numpy.mean(nan_strip);
		
				if cor_phase_stat < 0.5:
					corrected_phases = [[0]*len(variants),[1]*len(variants)];
				elif cor_phase_stat > 0.5:
					corrected_phases = [[1]*len(variants),[0]*len(variants)];
				else:
					# no consensus, use population phasing
					print_warning("No GW phasing consensus for %s using method 1"%(str(variants)));
				
				cor_phase_stat = max([cor_phase_stat, 1-cor_phase_stat]);
					
			elif args.gw_phase_method == 1:
				# phase using MAF weighted phase
				# we need the mafs for this, so we need to look them up
				# Step 2 get allele frequencies
				# first get the allele frequency for each of the variants
				haplotype_mafs = [];
				for variant in variants:
					if os.path.isfile(args.gw_af_vcf) == True:
						try:
							var_chrom = dict_variant_reads[variant]['chr'];
							var_pos = dict_variant_reads[variant]['pos'];
							records = vcf_af.fetch(var_chrom,var_pos-1,var_pos);
							for record in records:
								if record.POS == var_pos:
									# get MAF for each allele;
									var_alleles = list(map(str,record.ALT));
									if args.gw_af_field in record.INFO:
										var_afs = map(float, record.INFO[args.gw_af_field]);
									else:
										fatal_error("Field %s not found in %s, pleasure ensure --gw_af_field is set correctly"%(args.gw_af_field,args.gw_af_vcf));
									var_mafs = [];
									for var_allele, var_af in zip(var_alleles, var_afs):
										if var_allele in dict_variant_reads[variant]['alleles']:
											var_mafs.append(min([1-var_af,var_af]));
							if len(var_mafs) > 0:
								haplotype_mafs.append(min(var_mafs));
							else:
								haplotype_mafs.append(0);
						except:
							print_warning("AF Lookup failed (2) for %s:%d"%(var_chrom,var_pos));
					else:
						haplotype_mafs.append(dict_variant_reads[variant]['maf']);
						
				if len(haplotype_mafs) == len(variants):
					phase_support = [0,0];
					# now we need to weight the phasing by their MAF
					for phase, maf in zip(phases[0],haplotype_mafs):
						if phase == 0:
							phase_support[0] += maf;
						elif phase == 1:
							phase_support[1] += maf;
				
					# now select the phase with the most MAF support
					if sum(phase_support) > 0:
						cor_phase_stat = max(phase_support) / sum(phase_support);
						
						if phase_support[0] > phase_support[1]:
							corrected_phases = [[0]*len(variants),[1]*len(variants)];
						elif phase_support[1] > phase_support[0]:
							corrected_phases = [[1]*len(variants),[0]*len(variants)];
						else:
							# no consensus, use population phasing
							print_warning("No GW phasing consensus for %s using method 2"%(str(variants)));
					else:
						# variants are not found in AF VCF but they still have  phase, try using other approach
						# phase using most common phase
						cor_phase_stat = numpy.mean(nan_strip);
	
						if cor_phase_stat < 0.5:
							corrected_phases = [[0]*len(variants),[1]*len(variants)];
						elif cor_phase_stat > 0.5:
							corrected_phases = [[1]*len(variants),[0]*len(variants)];
						else:
							# no consensus, use population phasing
							print_warning("No GW phasing consensus for %s using method 1"%(str(variants)));
			
						cor_phase_stat = max([cor_phase_stat, 1-cor_phase_stat]);
				else:
					print_warning("GW phasing failed for %s"%(str(variants)));
				
		# save the stat for lookup when generating VCF
		haplotype_gw_stat_lookup[list_to_string(variants)] = cor_phase_stat;
		
		# update the variants with their corrected phases
		for var_index in range(0,len(variants)):
			variant = variants[var_index];
			allele_index = dict_variant_reads[variant]['alleles'].index(alleles[0][var_index])
			dict_variant_reads[variant]['gw_phase'][allele_index] = corrected_phases[0][var_index];
			dict_variant_reads[variant]['gw_phase'][1-allele_index] = corrected_phases[1][var_index];
		
		corrected_phase_string = ["",""]
		corrected_phase_string[0] = "".join([str(x).replace("nan", "-") for x in corrected_phases[0]]);
		corrected_phase_string[1] = "".join([str(x).replace("nan", "-") for x in corrected_phases[1]]);
		
		## write the haplotype details
		stream_out.write(str_join("\t",[chrs[0],min(positions),max(positions),max(positions)-min(positions),len(variants),list_to_string(rsids),list_to_string(alleles[0])+"|"+list_to_string(alleles[1]),hap_counts[0],hap_counts[1],sum(hap_counts),supporting_connections,total_connections,phase_string[0]+"|"+phase_string[1],phase_concordant,corrected_phase_string[0]+"|"+corrected_phase_string[1],cor_phase_stat])+"\n");
		
		#$ write ASE stats
		
		# generate haplotypic counts
		set_hap_expr_reads = [[],[]];
		hap_expr_counts = [0,0];
		
		for hap_index in range(0,2):
			hap_x = [haplotype_a, haplotype_b][hap_index];
			
			for var_index in range(0, len(variants)):
				id = variants[var_index];
				chrom = dict_variant_reads[id]['chr'];
				pos = int(dict_variant_reads[id]['pos']);
				
				# check to see if variant is blacklisted
				if chrom in dict_haplo_blacklist_interval:
					blacklist = dict_haplo_blacklist_interval[chrom][pos-1:pos];
				else:
					blacklist = [];

				if len(blacklist) == 0:
					allele = dict_variant_reads[id]['alleles'][int(hap_x[var_index])];
					allele_index = dict_variant_reads[id]['alleles'].index(allele);
					
					if len(haplo_count_bam_indices) == 0:
						# if no BAM has been selected for haplo counts just use the sum across all BAMs
						set_hap_expr_reads[hap_index] += dict_variant_reads[id]['read_set'][allele_index];
					else:
						# otherwise specifically use the counts from the specified BAM(s)
						set_hap_expr_reads[hap_index] += dict_variant_reads[id]['haplo_reads'][allele_index];
				
			set_hap_expr_reads[hap_index] = list(set(set_hap_expr_reads[hap_index]));
			hap_expr_counts[hap_index] = len(set_hap_expr_reads[hap_index]);
		
		hap_a_count = hap_expr_counts[0];
		hap_b_count = hap_expr_counts[1]
		hap_a_reads = set_hap_expr_reads[0];
		hap_b_reads = set_hap_expr_reads[1];
	
		total_cov = int(hap_a_count)+int(hap_b_count);
		
		if 	total_cov >= args.min_cov:
			out_block_gw_phase = "0/1";
			if corrected_phases[0][0] == 0:
				# haplotype A = GW phase 0
				out_block_gw_phase = "0|1";
			elif corrected_phases[0][0] == 1:
				# haplotype A = GW phase 1
				out_block_gw_phase = "1|0";
			
			fields_out = [chrs[0],min(positions),max(positions),list_to_string(variants),len(variants),list_to_string(alleles[0]),list_to_string(alleles[1]),hap_a_count,hap_b_count,total_cov,out_block_gw_phase,cor_phase_stat];
			if args.output_read_ids == 1:
				fields_out += [list_to_string(hap_a_reads),list_to_string(hap_b_reads)];
			
			stream_out_ase.write(str_join("\t",fields_out)+"\n");
		
		## OUTPUT THE NETWORK FOR A SPECIFIC HAPLOTYPE
		if args.output_network in variants:
			#hap_a_network = generate_hap_network([variants, haplotype_a])[0];
			#hap_b_network = generate_hap_network([variants, haplotype_b])[0];
			hap_a_network = generate_hap_network_all(variants)[0];
			stream_out_network = open(args.o+".network.links.txt","w");
			stream_out_network.write("\t".join(["variantA","variantB","connections","inferred\n"]));
			nodes = [];
			#for item in hap_a_network + hap_b_network:
			hap_a_vars = [];
			for item in hap_a_network:
				if item[2] > 0:
					stream_out_network.write(list_to_string(item,"\t")+"\n");
					nodes.append(item[0]);
					nodes.append(item[1]);
			
			stream_out_network.close();
			stream_out_network = open(args.o+".network.nodes.txt","w");
			stream_out_network.write("id\tindex\tassigned_hap\n");
			for item in set(nodes):
				xvar = item.split(":")[0];
				xallele = item.split(":")[1];
				var_index = variants.index(xvar);
				if alleles[0][var_index] == xallele:
					assigned_hap = "A";
				else:
					assigned_hap = "B";
				stream_out_network.write(item+"\t"+str(var_index)+"\t"+assigned_hap+"\n");
			stream_out_network.close()
		
		## OUTPUT allele configuration
		for variant_a, allele_a in zip(variants, alleles[0]):
			for variant_b, allele_b in zip(variants, alleles[1]):
				if variant_a != variant_b:
					a_config = "";
					if (dict_variant_reads[variant_a]['ref'] == allele_a and dict_variant_reads[variant_b]['ref'] == allele_b) or (dict_variant_reads[variant_a]['ref'] != allele_a and dict_variant_reads[variant_b]['ref'] != allele_b):
						# ref and ref are in trans
						# this is a compound het
						a_config = "trans";
					elif (dict_variant_reads[variant_a]['ref'] == allele_a and dict_variant_reads[variant_b]['ref'] != allele_b) or (dict_variant_reads[variant_a]['ref'] != allele_a and dict_variant_reads[variant_b]['ref'] == allele_b):
						# ref and ref are in cis
						a_config = "cis";
					if a_config != "":
						stream_out_allele_configs.write("\t".join([variant_a,dict_variant_reads[variant_a]['rsid'],variant_b,dict_variant_reads[variant_b]['rsid'],a_config])+"\n");
		
	#output read counts for unphased variants
	if args.unphased_vars == 1:
		singletons = set(dict_variant_reads.keys()) - set(all_variants);
	
		for variant in singletons:
			dict_var = dict_variant_reads[variant];
			chrom = dict_var['chr'];
			pos = int(dict_var['pos']);
				
			# check to see if variant is blacklisted
			if chrom in dict_haplo_blacklist_interval:
				blacklist = dict_haplo_blacklist_interval[chrom][pos-1:pos];
			else:
				blacklist = [];

			if len(blacklist) == 0:				
				if len(haplo_count_bam_indices) == 0:
					# if no BAM has been selected for haplo counts just use the sum across all BAMs
					hap_a_count = len(dict_var['read_set'][0]);
					hap_b_count = len(dict_var['read_set'][1]);
					hap_a_reads = dict_var['read_set'][0];
					hap_b_reads = dict_var['read_set'][1];
				else:
					# otherwise specifically use the counts from the specified BAM(s)
					hap_a_count = len(set(dict_var['haplo_reads'][0]));
					hap_b_count = len(set(dict_var['haplo_reads'][1]));
					hap_a_reads = set(dict_var['haplo_reads'][0]);
					hap_b_reads = set(dict_var['haplo_reads'][1]);
			
				total_cov = int(hap_a_count)+int(hap_b_count);
				if total_cov >= args.min_cov:
					if "-" not in dict_var['phase']:
						phase_string = str(dict_var['phase'].index(dict_var['alleles'][0]))+"|"+str(dict_var['phase'].index(dict_var['alleles'][1]));
					else:
						phase_string = "0/1";
					fields_out = [dict_var['chr'],str(dict_var['pos']),str(dict_var['pos']),variant,str(1),dict_var['alleles'][0],dict_var['alleles'][1],str(hap_a_count),str(hap_b_count),str(total_cov),phase_string,"1"];
				
					if args.output_read_ids == 1:
						fields_out += [list_to_string(hap_a_reads),list_to_string(hap_b_reads)];
			
					stream_out_ase.write("\t".join(fields_out)+"\n");
	
		#output haplotypes for unphased variants (if enabled)
		for variant in singletons:
			dict_var = dict_variant_reads[variant];
			total_cov = len(dict_var['read_set'][0])+len(dict_var['read_set'][1]);
			
			# make sure it is actually phased
			if "-" not in dict_var['phase']:
				phase_string = str(dict_var['phase'].index(dict_var['alleles'][0]))+"|"+str(dict_var['phase'].index(dict_var['alleles'][1]));
			else:
				phase_string = "-|-";
			
			stream_out.write(dict_var['chr']+"\t"+str(dict_var['pos']-1)+"\t"+str(dict_var['pos'])+"\t"+str(1)+"\t"+str(1)+"\t"+variant+"\t"+dict_var['alleles'][0]+"|"+dict_var['alleles'][1]+"\t"+str(len(dict_var['read_set'][0]))+"\t"+str(len(dict_var['read_set'][1]))+"\t"+str(total_cov)+"\t"+str(0)+"\t"+str(0)+"\t"+phase_string+"\t"+str(float('nan'))+"\t"+phase_string+"\t"+str(float('nan'))+"\n");
			
	stream_out.close();
	stream_out_ase.close();
	stream_out_allele_configs.close();
	
	# output VCF
	if args.write_vcf == 1:
		unphased_phased, phase_corrected = write_vcf();
		
	total_time = time.time() - start_time;
	
	fun_flush_print("COMPLETED using %d reads in %d seconds using %d threads"%(total_reads,total_time,args.threads));
	fun_flush_print("     PHASED  %d of %d all variants (= %f) with at least one other variant"%(len(all_variants),het_count,float(len(all_variants))/float(het_count)));
	if args.write_vcf == 1:
		if unphased_count > 0:
			fun_flush_print("     GENOME WIDE PHASED  %d of %d unphased variants (= %f)"%(unphased_phased,unphased_count,float(unphased_phased)/float(unphased_count)));
		fun_flush_print("     GENOME WIDE PHASE CORRECTED  %d of %d variants (= %f)"%(phase_corrected,het_count,float(phase_corrected)/float(het_count)));
				
def generate_connectivity_map(chrom):
	global read_vars;
	global dict_variant_reads;
	
	dict_variant_overlap = {};
	
	for read_id in read_vars[chrom].keys():
		overlapped_variants = read_vars[chrom][read_id];
		for variant in overlapped_variants:
			var_chr = dict_variant_reads[variant]['chr'];
			for other_variant in overlapped_variants:
				other_var_chr = dict_variant_reads[other_variant]['chr'];
				# Restrict to being on the same chromosome, speeds up and allows parallelization
				# might not be desired for some very specific cases (ie trans-splicing)
				if var_chr == other_var_chr and other_variant != variant:
					if var_chr not in dict_variant_overlap: dict_variant_overlap[var_chr] = {};
					if variant not in dict_variant_overlap[var_chr]: dict_variant_overlap[var_chr][variant] = [];
					dict_variant_overlap[var_chr][variant].append(other_variant);
	
	return(dict_variant_overlap);

def process_mapping_result(input):
	global use_as_cutoff;
	global as_cutoff;
	global bam_index;
	global haplo_count_bam_indices;
	
	dict_variant_reads = {};
	read_vars = {};
	stream_in = open(input, "r");
	total_reads = 0;
	chrom = "";
	mapped_reads = 0;

	for line in stream_in:
		fields = line.rstrip().split("\t");
		#read_name	variant_id	rs_id	read_allele	alignment_score	genotype	maf
		if use_as_cutoff == False or int(fields[4]) >= as_cutoff:
			read_id = fields[0];
			var_id = fields[1];
			chrom = var_id.split(args.id_separator)[0];
			# add to the quick lookup dictionary
			if read_id not in read_vars: read_vars[read_id] = [];
			read_vars[read_id].append(var_id);
			
			if var_id not in dict_variant_reads: dict_variant_reads[var_id] = generate_variant_dict(fields);
			read_allele = fields[3];
			if read_allele in dict_variant_reads[var_id]['alleles']:
				allele_index = dict_variant_reads[var_id]['alleles'].index(read_allele)
				dict_variant_reads[var_id]['reads'][allele_index].append(read_id);
				mapped_reads += 1;
				if bam_index in haplo_count_bam_indices or len(haplo_count_bam_indices) == 0:
					dict_variant_reads[var_id]['haplo_reads'][allele_index].append(read_id);
			else:
				dict_variant_reads[var_id]['other_reads'].append(read_id);
			total_reads += 1;
	stream_in.close();
	
	return([dict_variant_reads,read_vars,total_reads, chrom]);
	
def call_mapping_script(input):
	global args;
	global devnull;
	
	chrom = input[0];
	bed_out = input[1];
	mapper_out = input[2];
	samtools_arg = input[3];
	bam = input[4];
	mapq = input[5];
	isize = input[6];
	
	mapping_result = tempfile.NamedTemporaryFile(delete=False);
	mapping_result.close();
	
	#Save error code from subprocess if not 0, file it writes is truncated and gives unexpected wrong results.
	error_code = subprocess.call("samtools view -h "+bam+" "+chrom+": | samtools view -Sh "+samtools_arg+" -L "+bed_out+" -q "+mapq+" - | "+args.python_string+" "+return_script_path()+"/call_read_variant_map.py --baseq "+str(args.baseq)+" --splice 1 --isize_cutoff "+str(isize)+" --variant_table "+mapper_out+" --o "+mapping_result.name, stderr=devnull, stdout=devnull, shell=True);
	if error_code != 0:
		raise RuntimeError("subprocess.call of call_read_variant_map.py exited with an error")
		
	fun_flush_print("               completed chromosome %s..."%(chrom));
	return(mapping_result.name);

def generate_mapping_table(input):
	global args;
	
	chrom = input[0];
	chrom = args.chr_prefix + chrom;
	
	vcf_lines = input[1];
	bed_out = tempfile.NamedTemporaryFile(delete=False);
	mapper_out = tempfile.NamedTemporaryFile(delete=False);
	het_count = 0;
	total_indels_excluded = 0;
	
	for vcf_columns in vcf_lines:
		pos = vcf_columns[1];
		rs_id = vcf_columns[2];
		alt_alleles = vcf_columns[4].split(",");
		all_alleles = [vcf_columns[3]] + alt_alleles;
		unique_id = chrom+args.id_separator+pos+args.id_separator+(args.id_separator.join(all_alleles));
		
		geno_string = vcf_columns[9];
		genotype = vcf_columns[10];
		
		maf = None;
		if args.gw_phase_method == 1:
			info_fields = annotation_to_dict(vcf_columns[7])
			if args.gw_af_field in info_fields:
				# make sure to get the right index if multi-allelic site
				afs = map(float, info_fields[args.gw_af_field].split(","));
				
				# make sure that there are the same number of allele frequencies as alternative variants			
				if len(afs) == len(alt_alleles):
					use_afs = [];
					for allele in list(genotype):
						if allele != "." and int(allele) != 0:
							use_afs.append(int(allele) - 1);
					# if there are multiple alternative alleles use the lowest MAF
					if len(use_afs) > 0:
						maf = min([min([afs[x],1-afs[x]]) for x in use_afs]);
	
		max_allele_size = max([len(x) for x in all_alleles]);
	
		if (max_allele_size == 1 or args.include_indels == 1):
				mapper_out.write("\t".join([chrom,vcf_columns[1],unique_id,rs_id,",".join(all_alleles),str(len(vcf_columns[3])),geno_string, str(maf)])+"\n");
				bed_out.write("\t".join([chrom,str(int(vcf_columns[1])-1),vcf_columns[1]])+"\n");
				het_count += 1;
		else:
			total_indels_excluded += 1;
				
	bed_out.close();
	mapper_out.close();
	
	return([chrom, het_count, total_indels_excluded, bed_out.name, mapper_out.name]);

def return_script_path():
    return os.path.dirname(os.path.realpath(sys.argv[0]));
    
def generate_variant_dict(fields):
	#read_name	variant_id	rs_id	read_allele	alignment_score	genotype	maf
	id_split = fields[1].split(args.id_separator);
	all_alleles = id_split[2:len(id_split)];
	
	genotype = list(fields[5]);
	is_phased = 0;
	if "|" in genotype:
		genotype.remove("|");
		is_phased = 1;
	if "/" in genotype: genotype.remove("/");

	# get only the alleles this individual has
	ind_alleles = [];

	for i in range(0,len(all_alleles)):
		if str(i) in genotype:
			ind_alleles.append(all_alleles[i]);
	
	# get phasing
	phase = [];
	if is_phased == 1:
		for index in genotype:
			phase.append(all_alleles[int(index)]);
	else:
		phase = ["-","-"];
	
	maf = fields[6];
	try:
		maf = float(maf);
	except:
		maf = 0;
	
	return({"id":fields[1], "rsid":fields[2],"ref":all_alleles[0],"chr":id_split[0],"pos":int(id_split[1]),"alleles":ind_alleles,"phase":phase, "gw_phase":phase, "maf":maf, "other_reads":[], "reads":[[] for i in range(len(ind_alleles))], "haplo_reads":[[] for i in range(len(ind_alleles))]});
	
def phase_block_container(input):
	#stream_out = open(input[0],"w");
	output = [];
	for i in input:
		output.append(phase_block(i));
	#for block in phase_block_result:
	#	stream_out.write(",".join(block)+"\n");
	#stream_out.close();
	
	return(output);

def phase_block(input):
	global args;
	variants = input[0];
	variant_connections = copy.deepcopy(input[1]);
	allele_connections = copy.deepcopy(input[2]);
	largest_block = [];
	
	# first get all variants that have more than one connection
	multi_connected_variants = [];
	for variant in variant_connections:
		if len(variant_connections[variant]) > 1:
			multi_connected_variants.append(variant);
	
	# now see how many possible connections there are to remove
	# can only remove connections between two multiconnected variants
	removable_connections = [];
	for variant in multi_connected_variants:
		connections = variant_connections[variant];
		for connection in connections:
			if connection in multi_connected_variants:
				if connection+"|"+variant not in removable_connections and variant+"|"+connection not in removable_connections:
					removable_connections.append(variant+"|"+connection);
	
	remove_number = 0;
	remove_connections = [""];
	
	while remove_number <= len(removable_connections):
		# prune connections
		# add first no connection removal
		to_remove = list(itertools.combinations(range(len(removable_connections)), remove_number));
		if len(to_remove) > args.max_prune:
			print_warning("maximum number of pruning iterations reached for %s"%(variants));
			break;
		else:
			remove_connections += to_remove;
		remove_number += 1;
		
	for remove in remove_connections:
		remaining_hap_pool = copy.deepcopy(input[2]);
		for remove_index in remove:
			remove_keys = removable_connections[remove_index].split("|");
		
			if remove_keys[0]+":0" in remaining_hap_pool[remove_keys[1]+":0"]: remaining_hap_pool[remove_keys[1]+":0"].remove(remove_keys[0]+":0")
			if remove_keys[0]+":1" in remaining_hap_pool[remove_keys[1]+":0"]: remaining_hap_pool[remove_keys[1]+":0"].remove(remove_keys[0]+":1")
			if remove_keys[0]+":0" in remaining_hap_pool[remove_keys[1]+":1"]: remaining_hap_pool[remove_keys[1]+":1"].remove(remove_keys[0]+":0")
			if remove_keys[0]+":1" in remaining_hap_pool[remove_keys[1]+":1"]: remaining_hap_pool[remove_keys[1]+":1"].remove(remove_keys[0]+":1")
		
			if remove_keys[1]+":0" in remaining_hap_pool[remove_keys[0]+":0"]: remaining_hap_pool[remove_keys[0]+":0"].remove(remove_keys[1]+":0")
			if remove_keys[1]+":1" in remaining_hap_pool[remove_keys[0]+":0"]: remaining_hap_pool[remove_keys[0]+":0"].remove(remove_keys[1]+":1")
			if remove_keys[1]+":0" in remaining_hap_pool[remove_keys[0]+":1"]: remaining_hap_pool[remove_keys[0]+":1"].remove(remove_keys[1]+":0")
			if remove_keys[1]+":1" in remaining_hap_pool[remove_keys[0]+":1"]: remaining_hap_pool[remove_keys[0]+":1"].remove(remove_keys[1]+":1")
		
		set_remaining_hap_pool = set(remaining_hap_pool.keys());	
		while len(remaining_hap_pool) > 0:
			# this will construct many iterations of the same haplotype need to filter it out;
			# start the process with a variant pair;
			seed_var = remaining_hap_pool.keys()[0];
			seed = set([seed_var] + list(remaining_hap_pool[seed_var]));
			del remaining_hap_pool[seed_var];
			set_remaining_hap_pool.remove(seed_var);
			result = build_haplotype_v3(seed,remaining_hap_pool,set_remaining_hap_pool);
			new_hap = list(result[0]);
			# sort by location
			new_hap = sort_var_ids(new_hap);
			remaining_hap_pool = result[1];
			set_remaining_hap_pool = result[2];
			if len(new_hap) > len(largest_block) and test_loop_back(new_hap) == 0:
				largest_block = new_hap;
			
		# check to see if we have a full haplotype
		if len(largest_block) == len(variants):
			return([largest_block]);
		
	# if we get here we failed to find a full block, so just return the best one and try to phase the remainder
	# remove phased variants from connections
	unphased_vars = [];
	unphased_var_connections = {};
	
	for variant in variants:
		if variant+":0" in largest_block or variant+":1" in largest_block:
			del allele_connections[variant+":0"];
			del allele_connections[variant+":1"];
			for connected_var in allele_connections:
				if variant+":0" in allele_connections[connected_var]: allele_connections[connected_var].remove(variant+":0");
				if variant+":1" in allele_connections[connected_var]: allele_connections[connected_var].remove(variant+":1");
		else:
			unphased_vars.append(variant);
			unphased_var_connections[variant] = [];
			if variant in variant_connections:
				for connection in variant_connections[variant]:
					if connection+":0" not in largest_block and connection+":1" not in largest_block:
						unphased_var_connections[variant].append(connection);
					
	#print(largest_block);
	#print("FAILED TO RESOLVE HAPLOTYPE: %s"%(input[1]));
	if len(unphased_vars) > 1:
		if len(largest_block) == 0:
			print_warning("phasing failed for %s. Attempted to remove %d combinations of %d connections"%(variants,len(list(remove_connections)),remove_number));
			return([[]]);
		else:
			print_warning("failed to phase full haplotype for %s, splitting into fragments, max haplotype = %s"%(variants,largest_block));
			return([largest_block]+phase_block([unphased_vars,unphased_var_connections,allele_connections]));
	else:
		return([largest_block]+[[unphased_vars[0]+":0"]]);

def test_loop_back(block):
	# test to see if a haplotype block ever includes the same variant more than once
	# strip allele
	block = [x.split(":")[0] for x in block];
	# count occurance of each variant
	counts = [block.count(x) for x in set(block)];
	
	if max(counts) == 1:
		return(0);
	else:
		return(1);
		
def test_variant_connection(input):
	global noise_e;
	global dict_variant_reads;
	
	chr, variant_a, variant_b = input;
	
	# there are only two possible configurations, determine evidence for each
	# a[ref]b[ref] | a[alt]b[alt]
	hap_config_a_support = len(dict_variant_reads[variant_a]['read_set'][0] & dict_variant_reads[variant_b]['read_set'][0]) + len(dict_variant_reads[variant_a]['read_set'][1] & dict_variant_reads[variant_b]['read_set'][1])
	# a[ref]b[alt] | a[alt]b[ref]
	hap_config_b_support = len(dict_variant_reads[variant_a]['read_set'][1] & dict_variant_reads[variant_b]['read_set'][0]) + len(dict_variant_reads[variant_a]['read_set'][0] & dict_variant_reads[variant_b]['read_set'][1])
		
	# determine if phasing is concordant with what as specified in the input VCF
	phase_concordant = ".";
	# make sure the input VCF had phase
	if "-" not in dict_variant_reads[variant_a]['phase'] and "-" not in dict_variant_reads[variant_b]['phase']:
		if hap_config_a_support > hap_config_b_support:
			
			if dict_variant_reads[variant_a]['phase'].index(dict_variant_reads[variant_a]['alleles'][0]) == dict_variant_reads[variant_b]['phase'].index(dict_variant_reads[variant_b]['alleles'][0]):
				phase_concordant = 1;
			else:
				phase_concordant = 0;
		elif hap_config_a_support < hap_config_b_support:
			if dict_variant_reads[variant_a]['phase'].index(dict_variant_reads[variant_a]['alleles'][1]) == dict_variant_reads[variant_b]['phase'].index(dict_variant_reads[variant_b]['alleles'][0]):
				phase_concordant = 1;
			else:
				phase_concordant = 0;

	# also get the connections from reads where the bases did not match to either ref or alt
	# a[other] -> b[ref]
	other_base_connections = len(dict_variant_reads[variant_a]['other_read_set'] & dict_variant_reads[variant_b]['read_set'][0]);
	# a[other] -> b[alt]
	other_base_connections += len(dict_variant_reads[variant_a]['other_read_set'] & dict_variant_reads[variant_b]['read_set'][1]);
	# a[ref] -> b[other]
	other_base_connections += len(dict_variant_reads[variant_a]['read_set'][0] & dict_variant_reads[variant_b]['other_read_set']);
	# a[alt] -> b[other]
	other_base_connections += len(dict_variant_reads[variant_a]['read_set'][1] & dict_variant_reads[variant_b]['other_read_set']);
	# a[other] -> b[other]
	other_base_connections += len(dict_variant_reads[variant_a]['other_read_set'] & dict_variant_reads[variant_b]['other_read_set']);
	
	c_supporting = max(hap_config_a_support,hap_config_b_support);
	c_total = hap_config_a_support + hap_config_b_support + other_base_connections;
	
	if hap_config_a_support > hap_config_b_support:
		chosen_config = 0;
	elif hap_config_a_support < hap_config_b_support:
		chosen_config = 1;
	else:
		chosen_config = -1;
	
	# if no reads support the phase then strip this connection
	if c_supporting == 0:
		conflicting_config_p = 0;
	elif c_total - c_supporting > 0:
		# otherwise do the test
		conflicting_config_p = binom.cdf(c_supporting,c_total,1-((6*noise_e)+(10*math.pow(noise_e,2))));
	else:
		# only both doing the test if there are some conflicting reads
		conflicting_config_p = 1;
		
	return([chr,variant_a,variant_b,conflicting_config_p,c_supporting,c_total,phase_concordant,chosen_config]);

def new_temp_file():
	xfile = tempfile.NamedTemporaryFile(delete=False)
	xfile.close();
	return(xfile.name);

def write_vcf():
	global args;
	global haplotype_lookup;
	global dict_variant_reads;
	global haplotype_pvalue_lookup
	global sample_column;
	
	fun_flush_print("#7. Outputting phased VCF...");	
	
	if args.gw_phase_vcf == 1:
		fun_flush_print("     GT field is being updated with phASER genome wide phase when applicable. This can be changed using the --gw_phase_vcf argument.");
	elif args.gw_phase_vcf == 2:
		fun_flush_print("     GT field is being updated with either phASER genome wide phase or phASER block phase with PS specified, depending on phase anchoring quality.");
	else:
		fun_flush_print("     GT field is not being updated with phASER genome wide phase. This can be changed using the --gw_phase_vcf argument.");
	
	if args.chr != "":
		decomp_str = "tabix -h "+args.vcf+" "+args.chr+":"
	else:
		decomp_str = "gunzip -c "+args.vcf;
	
	tmp_out = tempfile.NamedTemporaryFile(delete=False);
	tmp_out.close();
		
	subprocess.call(decomp_str + " | cut -f 1-9,"+str(sample_column+1)+" > "+tmp_out.name,shell=True);
		
	vcf_in = open(tmp_out.name,"r");
	vcf_out = gzip.open(args.o+".vcf.gz","w");
	
	phase_corrections = 0;
	unphased_phased = 0;
	
	set_phased_vars = set(haplotype_lookup.keys());
	format_section = False;
	format_written = False;
	
	for line in vcf_in:
		if "##FORMAT" in line:
			format_section = True;
		elif format_section == True and format_written == False:
			# insert new format info here
			vcf_out.write("##FORMAT=<ID=PG,Number=1,Type=String,Description=\"phASER Local Genotype\">\n");
			vcf_out.write("##FORMAT=<ID=PB,Number=1,Type=String,Description=\"phASER Local Block\">\n");
			vcf_out.write("##FORMAT=<ID=PI,Number=1,Type=String,Description=\"phASER Local Block Index (unique for each block)\">\n");
			vcf_out.write("##FORMAT=<ID=PW,Number=1,Type=String,Description=\"phASER Genome Wide Genotype\">\n");
			vcf_out.write("##FORMAT=<ID=PC,Number=1,Type=String,Description=\"phASER Genome Wide Confidence\">\n");
			
			format_section = False;
			format_written = True;
			
		vcf_columns = line.replace("\n","").split("\t");
		if line.startswith("#CHROM"):
			# if multiple samples only output phased sample
			out_cols = vcf_columns[0:9] + [vcf_columns[9]];
			vcf_out.write("\t".join(out_cols)+"\n");
		elif line[0:1] == "#":
			vcf_out.write(line);
		else:
			##CHROM  POS     ID      REF     ALT     QUAL    FILTER  INFO    FORMAT  NA06986
			id = vcf_columns[2];
			chrom = vcf_columns[0];
			pos = int(vcf_columns[1]);
			
			if args.chr == "" or chrom == args.chr:
				if "GT" in vcf_columns[8]:
					gt_index = vcf_columns[8].split(":").index("GT");
					genotype = list(vcf_columns[9].split(":")[gt_index]);
				
					if "|" in genotype: genotype.remove("|");
					if "/" in genotype: genotype.remove("/");
			
					# get only the alleles this individual has
					alt_alleles = vcf_columns[4].split(",");
					all_alleles = [vcf_columns[3]] + alt_alleles;
					ind_alleles = [];

					for i in range(0,len(all_alleles)):
						if str(i) in genotype:
							ind_alleles.append(all_alleles[i]);
				
					# make sure there are as many entries in each sample as there should be before adding new columns
					# if there are entries missing add blanks
					n_fields = len(vcf_columns[8].split(":"));
					for i in range(9, len(vcf_columns)):
						sample_fields = len(vcf_columns[i].split(":"));
						if sample_fields != n_fields:
							missing_cols = n_fields - sample_fields;
							vcf_columns[i] += ":" * missing_cols;
				
					vcf_columns[8] += ":PG:PB:PI:PW:PC";
					
					#generate a unique id
					unique_id = chrom+args.id_separator+str(pos)+args.id_separator+(args.id_separator.join(all_alleles));
					
					if unique_id in set_phased_vars:					
						# retrieve the correct allele number of each allele
						# issue because if a site is multi-allelic it will be converted to 0/1 (ie 0/2 converted to 0/1)
						alleles_out = [];
						gw_phase_out = ["",""];
						block_index = haplotype_lookup[unique_id][2];
					
						for allele in haplotype_lookup[unique_id][1].split("|"):
							allele_base = dict_variant_reads[unique_id]['alleles'][int(allele)];
							vcf_allele_index = all_alleles.index(allele_base);
						
							# get the genome wide phase
							gw_phase = dict_variant_reads[unique_id]['gw_phase'][int(allele)]
							if isinstance(gw_phase, int) == True:
								gw_phase_out[gw_phase] = str(vcf_allele_index);
							
							alleles_out.append(str(vcf_allele_index));
					
						# get rsID for each of the variants on the haplotype
						variants_out = [];
						for variant in haplotype_lookup[unique_id][0]:
							# if ":" is in the rsid need to replace it, otherwise it will mess up output
							variants_out.append(dict_variant_reads[variant]['rsid'].replace(":","_"));
						# get the p-value, if there was one for the block
						# pval = haplotype_pvalue_lookup[list_to_string(haplotype_lookup[unique_id][0])];
						gw_stat = haplotype_gw_stat_lookup[list_to_string(haplotype_lookup[unique_id][0])];
							
						# if desired to overwrite input phase with GW phase, do it here
						if "-" not in gw_phase_out:
							xfields = vcf_columns[9].split(":");
							new_phase = "|".join(gw_phase_out);
							if gw_stat >= args.gw_phase_vcf_min_confidence:
								if "|" in xfields[gt_index] and xfields[gt_index] != new_phase: phase_corrections += 1;
								if "/" in xfields[gt_index] and xfields[gt_index] != "./." and xfields[gt_index] != new_phase: unphased_phased += 1;
								
								if args.gw_phase_vcf == 1 or args.gw_phase_vcf == 2:
									xfields[gt_index] = new_phase;
									vcf_columns[9] = ":".join(xfields);
							
							if args.gw_phase_vcf == 2 and gw_stat < args.gw_phase_vcf_min_confidence:
								xfields[gt_index] = "|".join(alleles_out);
								vcf_columns[9] = ":".join(xfields);
						
						if args.gw_phase_vcf == 2 and gw_stat < args.gw_phase_vcf_min_confidence:
							vcf_columns[8] += ":PS";
							vcf_columns[9] += ":"+"|".join(alleles_out)+":"+list_to_string(variants_out)+":"+str(block_index)+":"+"|".join(gw_phase_out)+":"+str(gw_stat)+":"+str(block_index);
						else:
							vcf_columns[9] += ":"+"|".join(alleles_out)+":"+list_to_string(variants_out)+":"+str(block_index)+":"+"|".join(gw_phase_out)+":"+str(gw_stat);
					else:
						vcf_columns[9] += ":"+"/".join(sorted(genotype))+":.:.:"+vcf_columns[9].split(":")[gt_index]+":."
				
				# if VCF contains multiple samples, only output the phased sample
				out_cols = vcf_columns[0:9] + [vcf_columns[9]];
				
				vcf_out.write("\t".join(out_cols)+"\n");
					
	vcf_out.close();
	os.remove(tmp_out.name);
	
	return([unphased_phased, phase_corrections]);
	
def str_join(joiner,list):
	list = map(str, list);
	return(joiner.join(list));
	
def build_haplotypes(input):
	
	dict_variant_overlap = copy.deepcopy(input);
	
	block_haplotypes = [];
	total_hap_pool = len(dict_variant_overlap);
	remaining_hap_pool = dict_variant_overlap;
	set_remaining_hap_pool = set(dict_variant_overlap.keys());
	while len(remaining_hap_pool) > 0:
		# this will construct many iterations of the same haplotype need to filter it out;
		# start the process with a variant pair;
		seed_var = remaining_hap_pool.keys()[0];
		seed = set([seed_var] + list(remaining_hap_pool[seed_var]));
		del remaining_hap_pool[seed_var];
		set_remaining_hap_pool.remove(seed_var);
		result = build_haplotype_v3(seed,remaining_hap_pool,set_remaining_hap_pool);
		new_hap = list(result[0]);
		# sort by location
		new_hap = sort_var_ids(new_hap);
		remaining_hap_pool = result[1];
		set_remaining_hap_pool = result[2];
		block_haplotypes.append(new_hap);
	
	return(block_haplotypes);

def sort_var_ids(ids):
	xsplit = [x.split(args.id_separator) for x in ids];
	xsort = sorted(xsplit, key = lambda x: (x[0], int(x[1])))
	return([args.id_separator.join(x) for x in xsort]);

def count_hap_junctions(block):
	counted = set([]);
	reads = [];
	
	for var_index in range(0,len(block)):
		for var_allele in range(0,2):
			for other_index in range(0, len(block)):
				if other_index != var_index:
					for other_allele in range(0,2):
						if (str(var_index)+":"+str(var_allele)+":"+str(other_index)+":"+str(other_allele) not in counted) and (str(other_index)+":"+str(other_allele)+":"+str(var_index)+":"+str(var_allele) not in counted):
							reads += list(dict_variant_reads[block[var_index]]['read_set'][var_allele] & dict_variant_reads[block[other_index]]['read_set'][other_allele]);
							counted.add(str(var_index)+":"+str(var_allele)+":"+str(other_index)+":"+str(other_allele));
	return([block,len(reads)]);

def count_hap_reads(input):
	block = input[0];
	configuration = input[1];
	parent_block = None;
	block_number = None;
	
	if len(input) == 4:
		parent_block = input[2];
		block_number = input[3];
		
	global dict_variant_reads;
	
	reads = [];
	counted = set([]);
	# sum up supporting reads between all configs
	for var_index in range(0,len(block)):
		for other_index in range(0, len(block)):
			if other_index != var_index:
				if (str(var_index)+":"+str(other_index) not in counted) and (str(other_index)+":"+str(var_index) not in counted):
					# the noise test should be done here, only pairs where the signal is above noise should be counted.
					reads += list(dict_variant_reads[block[var_index]]['read_set'][int(configuration[var_index])] & dict_variant_reads[block[other_index]]['read_set'][int(configuration[other_index])]);
					counted.add(str(var_index)+":"+str(other_index));
					
	return([block, configuration, len(reads), parent_block, block_number]);

def generate_hap_network_all(input):
	block = input;
	
	global dict_variant_reads;
	
	reads = [];
	counted = set([]);
	
	out_junctions = [];
	
	for var_index in range(0,len(block)):
		for other_index in range(0, len(block)):
			if other_index != var_index:
				for allele_index in range (0,2):
					for other_allele_index in range(0,2):
						if (str(var_index)+":"+str(allele_index)+":"+str(other_index)+":"+str(other_allele_index) not in counted) and (str(other_index)+":"+str(other_allele_index)+":"+str(var_index)+":"+str(allele_index) not in counted):
							junctions = list(dict_variant_reads[block[var_index]]['read_set'][allele_index] & dict_variant_reads[block[other_index]]['read_set'][other_allele_index]);
							out_junctions.append([dict_variant_reads[block[var_index]]['id']+":"+dict_variant_reads[block[var_index]]['alleles'][allele_index],dict_variant_reads[block[other_index]]['id']+":"+dict_variant_reads[block[other_index]]['alleles'][other_allele_index], len(junctions), 0]);
							out_junctions.append([dict_variant_reads[block[var_index]]['id']+":"+dict_variant_reads[block[var_index]]['alleles'][int(not allele_index)],dict_variant_reads[block[other_index]]['id']+":"+dict_variant_reads[block[other_index]]['alleles'][int(not other_allele_index)], len(junctions), 1]);
							counted.add(str(var_index)+":"+str(allele_index)+":"+str(other_index)+":"+str(other_allele_index));
	
	return([out_junctions, block]);
	
def generate_hap_network(input):
	block = input[0];
	configuration = input[1];
	
	global dict_variant_reads;
	
	reads = [];
	counted = set([]);
	
	out_junctions = [];
	
	# sum up supporting reads between all configs
	for var_index in range(0,len(block)):
		for other_index in range(0, len(block)):
			if other_index != var_index:
				if (str(var_index)+":"+str(other_index) not in counted) and (str(other_index)+":"+str(var_index) not in counted):
					
					## SHOULD FIRST CHECK TO MAKE SURE THIS ISN'T A READ PAIR THAT FAILED THE TEST
					# actually I don't think this matters, it will always choose the most supported phase
					
					junctions = list(dict_variant_reads[block[var_index]]['read_set'][int(configuration[var_index])] & dict_variant_reads[block[other_index]]['read_set'][int(configuration[other_index])]);
					out_junctions.append([dict_variant_reads[block[var_index]]['rsid']+":"+dict_variant_reads[block[var_index]]['alleles'][int(configuration[var_index])],dict_variant_reads[block[other_index]]['rsid']+":"+dict_variant_reads[block[other_index]]['alleles'][int(configuration[other_index])], len(junctions), 0]);
					out_junctions.append([dict_variant_reads[block[var_index]]['rsid']+":"+dict_variant_reads[block[var_index]]['alleles'][int(not int(configuration[var_index]))],dict_variant_reads[block[other_index]]['rsid']+":"+dict_variant_reads[block[other_index]]['alleles'][int(not int(configuration[other_index]))], len(junctions), 1]);
					counted.add(str(var_index)+":"+str(other_index));
					
	return([out_junctions, block, configuration]);
	
def get_allele_phase(allele, var_dict):
	
	try:
		return(var_dict['phase'].index(allele));
	except:
		return(float('nan'));

def build_haplotype_v3(set_haplotype, dict_all_associations, set_all_associations):
	global args;
	
	overlapping = set_haplotype & set_all_associations;
	
	while len(overlapping) > 0:
		for variant in overlapping:
			set_haplotype = set_haplotype | dict_all_associations[variant];
			del dict_all_associations[variant];
			set_all_associations.remove(variant);
		
		overlapping = set_haplotype & set_all_associations;
		
	return([set_haplotype, dict_all_associations, set_all_associations])

def get_var_pos(var_fields):
	return(int(var_fields.split(":")[0]));

def list_to_string(xlist,sep=","):
	string_out = "";
	for item in xlist:
		string_out += str(item) + sep;
	
	if len(sep) > 0:
		string_out = string_out[:-len(sep)];
	
	return(string_out);

def print_warning(text):
	if args.show_warning == 1:
		fun_flush_print(text);

def dict_from_info(info_field):
	out_dict = {};
	fields = info_field.split(";");
	for field in fields:
		sub_field = field.split("=");
		if len(sub_field) == 2:
			out_dict[sub_field[0]] = sub_field[1];
	
	return(out_dict);

def fun_flush_print(text):
	print(text);
	sys.stdout.flush();

def fatal_error(text):
	fun_flush_print("     FATAL ERROR: "+text);
	quit();

def print_debug(text):
	if args.debug == 1:
		fun_flush_print(text);

def pool_split(threads, data):
	global args;
	data_length = len(data);
	pool_input = [];
	
	# calculate pool size if all data is divided by number of threads
	optimal_pool_size = data_length/threads;
	
	# unfortunately due to OS limitations the maximum output but a given thread is limited
	# so the pool size can't be too enormous
	# see : http://bugs.python.org/issue8426
	# so limit it to at max some value (set at 100,000 by default)
	# this is probably conservative but I haven't checked out what the best number is yet
	
	pool_size = min([args.max_items_per_thread, optimal_pool_size]);
	
	if pool_size > 0:
		pool_inputs = data_length / pool_size;
	
		for i in range(0,pool_inputs):
			#last pool gets the remaining reads
			if i == (pool_inputs-1):
				pool_input.append(data[(i*pool_size):]);
			else:
				pool_input.append(data[(i*pool_size):((i+1)*pool_size)]);
	else:
		pool_input = [];
	
	return(pool_input);

def pool_setup(pool_input):
	global args;
	
	threads = min([len(pool_input),args.threads]);
	
	return (multiprocessing.Pool(processes=threads));

def parallelize(function, pool_input):
	global args;
	
	if len(pool_input) > 0:
		threads = min([len(pool_input),args.threads]);
		if args.threads > 1:
			pool = multiprocessing.Pool(processes=threads);
			pool_output = pool.map(function, pool_input);
			pool.close() # no more tasks
			pool.join()  # wrap up current tasks
		else:
			pool_output = [];
			for input in pool_input:
				pool_output.append(function(input));		
	else:
		pool_output = [];
		
	return(pool_output);

def annotation_to_dict(text,sep=";"):
	dict_out = {};
	vars = text.split(sep);
	for var in vars:
		if "=" in var:
			key = var.split("=")[0];
			values = var.split("=")[1];
			dict_out[key] = values;
	return(dict_out);

def phase_v3(input):
	global args;
	variants = input[0];
	variant_connections = input[1];
	allele_connections = input[2];
	
	
	# first check to see if haplotype is fully concordant
	# if it is simply return the haplotype
	xhap = resolve_phase(variants, allele_connections);
	if xhap != None:
		final_blocks = xhap;
	else:
		# if there is no concordant select phase with most support in terms of connections
	
		# first break up the block if needed into sublocks at weak points
		# always split where spanning connections = 1
		# then if needed subsequently split at 2, 3, 4, etc..
		if args.max_block_size == 0:
			xmax = len(variants);
		else:
			xmax = args.max_block_size;
	
		sub_blocks = split_by_weak(variants, variant_connections, xmax);
	
		# now select the most supported phase in each sub block
		if len(sub_blocks) == 1:
			sub_block_phases = [sub_block_phase(xvars,allele_connections)for xvars in sub_blocks];
		else:
			sub_block_phases = [sub_block_phase(xvars,allele_connections,attempt_resolve = True)for xvars in sub_blocks];
		# now phase sub blocks relative to each other
		# sequentially from the left
	
		split_phases = [];
		final_phase = sub_block_phases[0];
		split_start = 0;
	
		for i in range(1, len(sub_block_phases)):
			step_phases = [final_phase,sub_block_phases[i]];
			used_vars = sum([sum([len(y) for y in x]) for x in step_phases]) / 2;
			#print(used_vars);
			new_phase = sub_block_phase(variants[split_start:split_start+used_vars], allele_connections, step_phases);
			# if phasing including the next block includes uncertainty then need to split
			if "-" in new_phase[0]:	
				split_phases+= [final_phase];
				split_start = used_vars;
				final_phase = sub_block_phases[i];
			else:
				final_phase = new_phase;
		
		final_blocks = split_phases + [final_phase];
		do_print = 1;
	
	out_phase = [];
	variant_index = 0;
	for block in final_blocks:
		out_block = [];
		for allele in block[0]:
			out_block.append(variants[variant_index]+":"+allele)
			variant_index += 1;
		if "-" not in out_block[0].split(":")[1]:
			out_phase.append(out_block);
		
	return(out_phase);
		
def resolve_phase(variants, allele_connections, clean_connections = False):
	
	# if needed remove connections from allele_connections that are not in the variant list
	if clean_connections == True:
		set_variants = set(variants);
		cleaned_connections = {};
		for allele in allele_connections:
			variant = allele.split(":")[0];
			if variant in set_variants:
				cleaned_connections[allele] = set([]);
				for connection in allele_connections[allele]:
					other_variant = connection.split(":")[0];
					if other_variant in variants:
						cleaned_connections[allele].add(connection);
	
		allele_connections = cleaned_connections;
								
	remaining_hap_pool = copy.deepcopy(allele_connections);
	set_remaining_hap_pool = set(remaining_hap_pool.keys());	
	seed_var = remaining_hap_pool.keys()[0];
	seed = set([seed_var] + list(remaining_hap_pool[seed_var]));
	del remaining_hap_pool[seed_var];
	set_remaining_hap_pool.remove(seed_var);
	result = build_haplotype_v3(seed,remaining_hap_pool,set_remaining_hap_pool);
	new_hap = list(result[0]);
	if len(new_hap) == len(variants):
		output = "";
		for xvar in variants:
			if xvar+":0" in new_hap:
				output += "0";
			elif xvar+":1" in new_hap:
				output += "1";
		return([[output,inverse_conifg(output)]]);
	else:
		return(None);
	
def sub_block_phase(variants, allele_connections, sub_block_configs=[], attempt_resolve = False):
	
	if len(sub_block_configs) > 0:
		# if we given sub block phases then we are phasing sub blocks against each other
		configurations = [];
		configurations += [sub_block_configs[0][0] + sub_block_configs[1][0]];
		configurations += [sub_block_configs[0][0] + sub_block_configs[1][1]];
		configurations += [sub_block_configs[0][1] + sub_block_configs[1][0]];
		configurations += [sub_block_configs[0][1] + sub_block_configs[1][1]];
	else:
		# first try to resolve phase
		if attempt_resolve == True:
			xhap = resolve_phase(variants, allele_connections, clean_connections = True);
			if xhap != None:
				return(xhap[0]);
	
		# otherwise determine all possible configurations in this block
		configurations = ["".join(seq) for seq in itertools.product("01", repeat=len(variants))];
	
	supporting_connections = {};
	
	set_variants = set(variants);
		
	for configuration in configurations:
		inverse_config = inverse_conifg(configuration);
		# only test each haplotype once, not necessary to test complement
		if configuration + "|" + inverse_config not in supporting_connections and inverse_config + "|" + configuration not in supporting_connections:
			support = 0;
			for variant, allele in zip(variants, configuration):
				if allele != "-":
					if variant+":"+allele in allele_connections:
						for other_variant, other_allele in zip(variants, configuration):
							if other_variant != variant:
								if other_allele != "-":
									if other_variant+":"+other_allele in allele_connections[variant+":"+allele]:
										support += 1;
							
			supporting_connections[configuration + "|" + inverse_config] = support;
	
	# select connections with maximum support
	max_support = max(supporting_connections.values());
	
	best_configs = [];
	for config in supporting_connections.keys():
		if supporting_connections[config] == max_support:
			best_configs.append(config);
	
	if len(best_configs) == 1:
		return(best_configs[0].split("|"));
	else:
		return(["-"*len(variants),"-"*len(variants)]);

def inverse_conifg(config):
	out_config = "";
	
	for allele in config:
		if allele != "-":
			out_config += str(int(not int(allele)));
		else:
			out_config += "-";
	
	return(out_config)
	
def split_by_weak(variants, variant_connections, max_size):
	#NOTE THE INPUT VARIANT LIST MUST BE SORTED BY POSITION
	
	weak_points = find_weak_points(variants, variant_connections);
	# first always split at points only spanned by one connection, there is no reason not to
	haplo_fragments = [];
	split_points = [];
	split_at = 1;
	max_frag = len(variants);
	while max_frag > max_size or split_at == 1:
		for position in sorted(weak_points.keys()):
			if weak_points[position] == split_at:
				if position + 1 not in split_points and position - 1 not in split_points:
					split_points.append(position);
		
		if len(split_points) > 0:
			haplo_fragments = split_variants(variants, split_points);
		else:
			haplo_fragments = [variants];
			
		max_frag = max([len(x) for x in haplo_fragments]);
		split_at += 1;
	
	return(haplo_fragments);

def split_variants(variants, split_points):
	split_points = sorted(split_points);
	split_variants = [];
	for i in range(0,len(split_points)+1):
		if i == 0:
			split_variants.append(variants[:split_points[i]]);
		elif i < len(split_points):
			split_variants.append(variants[split_points[i-1]:split_points[i]]);
		else:
			split_variants.append(variants[split_points[i-1]:]);
	
	return(split_variants);

def find_weak_points(variants, variant_connections):
	# this function reports how many connections are crossing each point, where a point is between a pair of variants
	# it returns a dictionary with the counts at each point
	
	dict_counts = {};
	
	for position in range(2,len(variants)-1):
		dict_counts[position] = 0;
		
		for xvar in variant_connections:
			for connection in variant_connections[xvar]:
				# check if variant spans position
				if variants.index(xvar) < (position - 0.5) and variants.index(connection) > (position - 0.5):
					dict_counts[position] += 1;
	
	return(dict_counts);
	
def sample_column_map(path, start_col=9, line_key="#CHR"):
	stream_in = gzip.open(path, "r");
	
	out_map = {};
	for line in stream_in:
		if line_key in line:
			line = line.rstrip().split("\t");
			for i in range(start_col,len(line)):
				out_map[line[i]] = i;
		
			break;
	
	stream_in.close();
	
	return(out_map);
				
if __name__ == "__main__":
	main();
