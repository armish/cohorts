# Copyright (c) 2016. Mount Sinai School of Medicine
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

from os import path, makedirs
from shutil import rmtree
import pandas as pd
import pickle

import varcode
from varcode import VariantCollection
from mhctools import NetMHCcons
from topiary import predict_epitopes_from_variants, epitopes_to_dataframe

class Cohort(object):
    """Represents a cohort of patients."""

    def __init__(self,
                 data_dir,
                 cache_dir,
                 sample_ids,
                 normal_bam_ids,
                 tumor_bam_ids,
                 hla_alleles=None,
                 cache_results=True,
                 snv_file_format_funcs=None,
                 indel_file_format_funcs=None):
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        self.normal_bam_ids = normal_bam_ids
        self.tumor_bam_ids = tumor_bam_ids
        self.hla_alleles = hla_alleles
        self.cache_results = cache_results
        self.snv_file_format_funcs = snv_file_format_funcs
        self.indel_file_format_funcs = indel_file_format_funcs
        self.sample_ids = sample_ids

        variant_type_to_format_funcs = {}
        if self.snv_file_format_funcs is not None:
            variant_type_to_format_funcs["snv"] = self.snv_file_format_funcs
        if self.indel_file_format_funcs is not None:
            variant_type_to_format_funcs["indel"] = self.indel_file_format_funcs
        self.variant_type_to_format_funcs = variant_type_to_format_funcs

        self.variant_cache_name = "cached-variants"
        self.neoantigen_cache_name = "cached-neoantigens"

    def load_from_cache(self, cache_name, sample_id, file_name):
        if not self.cache_results:
            return None

        cache_dir = path.join(self.cache_dir, cache_name)
        sample_cache_dir = path.join(cache_dir, str(sample_id))
        cache_file = path.join(sample_cache_dir, file_name)

        if not path.exists(cache_file):
            return None

        if path.splitext(cache_file)[1] == ".csv":
            return pd.read_csv(cache_file)
        else:
            with open(cache_file, "rb") as f:
                return pickle.load(f)

    def save_to_cache(self, obj, cache_name, sample_id, file_name):
        if not self.cache_results:
            return

        cache_dir = path.join(self.cache_dir, cache_name)
        sample_cache_dir = path.join(cache_dir, str(sample_id))
        cache_file = path.join(sample_cache_dir, file_name)

        if not path.exists(sample_cache_dir):
            makedirs(sample_cache_dir)

        if type(obj) == pd.DataFrame:
            obj.to_csv(cache_file, index=False)
        else:
            with open(cache_file, "wb") as f:
                pickle.dump(obj, f)

    def load_variants(self, variant_type="snv", merge_type="union"):
        assert variant_type in ["snv", "indel"], "Unknown variant type: %s" % variant_type
        sample_variants = {}

        for i, sample_id in enumerate(self.sample_ids):
            try:
                variants = self._load_single_sample_variants(
                    i, self.variant_type_to_format_funcs[variant_type], variant_type, merge_type) 
            except IOError:
                print("Variants did not exist for %s" % sample_id)
                continue

            sample_variants[sample_id] = variants
        return sample_variants

    def _load_single_sample_variants(self, sample_idx, file_format_funcs, variant_type, merge_type):
        sample_id = self.sample_ids[sample_idx]
        normal_bam_id = self.normal_bam_ids[sample_idx]
        tumor_bam_id = self.tumor_bam_ids[sample_idx]

        cached_file_name = "%s-%s-variants.pkl" % (variant_type, merge_type)
        cached = self.load_from_cache(self.variant_cache_name, sample_id, cached_file_name)
        if cached is not None:
            return cached

        combined_variants = []
        for file_format_func in file_format_funcs:
            file_name = file_format_func(
                sample_id, normal_bam_id, tumor_bam_id)
            variants = varcode.load_vcf_fast(path.join(self.data_dir, file_name))
            combined_variants.append(set(variants.elements))

        if len(combined_variants) == 1:
            assert merge_type == None, "Cannot specify a merge type when there is nothing to merge"
            merged_variants =  VariantCollection(combined_variants)
        else:
            assert merge_type in ["union", "intersection"], "Unknown merge type: %s" % merge_type
            if merge_type == "union":
                merged_variants = VariantCollection(set.union(*combined_variants))
            elif merge_type == "intersection":
                merged_variants = VariantCollection(set.intersection(*combined_variants))

        self.save_to_cache(merged_variants, self.variant_cache_name, sample_id, cached_file_name)

        return merged_variants

    def load_neoantigens(self, variant_type="snv", merge_type="union",
                         epitope_lengths=[8, 9, 10, 11], ic50_cutoff=500,
                         process_limit=10, max_file_records=None):
        assert self.hla_alleles is not None, "Cannot predict neoantigens without HLA alleles"

        dfs = []
        for i, sample_id in enumerate(self.sample_ids):
            df_epitopes = self._load_single_sample_neoantigens(
                i, variant_type, merge_type, epitope_lengths, ic50_cutoff,
                process_limit, max_file_records)
            dfs.append(df_epitopes)
        return pd.concat(dfs)

    def _load_single_sample_neoantigens(self, sample_idx, variant_type, merge_type,
                                        epitope_lengths, ic50_cutoff, process_limit, max_file_records):
        sample_id = self.sample_ids[sample_idx]

        cached_file_name = "%s-%s-neoantigens.csv" % (variant_type, merge_type)
        cached = self.load_from_cache(self.neoantigen_cache_name, sample_id, cached_file_name)
        if cached is not None:
            return cached

        variants = self._load_single_sample_variants(
            sample_idx, self.variant_type_to_format_funcs[variant_type], variant_type, merge_type)
        hla_alleles = self.hla_alleles[sample_idx]
        mhc_model = NetMHCcons(
            alleles=hla_alleles,
            epitope_lengths=epitope_lengths,
            max_file_records=max_file_records,
            process_limit=process_limit)
        epitopes = predict_epitopes_from_variants(
            variants=variants,
            mhc_model=mhc_model,
            ic50_cutoff=ic50_cutoff,
            # Only include peptides with a variant
            only_novel_epitopes=True)
        df_epitopes = epitopes_to_dataframe(epitopes)
        df_epitopes["sample_id"] = sample_id

        self.save_to_cache(df_epitopes, self.neoantigen_cache_name, sample_id, cached_file_name)

        return df_epitopes

    def clear_caches(self):
        self.clear_variant_cache()
        self.clear_neoantigen_cache()

    def clear_cache(self, cache_name):
        cache_path = path.join(self.cache_dir, cache_name)
        if path.exists(cache_path):
            rmtree(cache_path)

    def clear_variant_cache(self):
        self.clear_cache(self.variant_cache_name)

    def clear_neoantigen_cache(self):
        self.clear_cache(self.neoantigen_cache_name)
