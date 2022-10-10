import os
import sys
import tempfile
import timeit
import traceback
import zipfile
from io import BytesIO

import xarray as xr
from dask.distributed import Client, LocalCluster, Lock, as_completed
from dask_cloudprovider.aws import FargateCluster

from hwpccalc.hwpc import model, model_data
from hwpccalc.hwpc.names import Names as nm
from hwpccalc.utils import singleton
from hwpccalc.utils.s3_helper import S3Helper

class MetaModel(singleton.Singleton):
    """ """

    def __new__(cls, *args, **kwargs):
        """MetaModel is designed to be a singleton, so instance variables are set
        up here for scheduling, tracking, and resolving model runs.
        """
        if MetaModel._instance is None:
            super().__new__(cls, args, kwargs)

            MetaModel.start = timeit.default_timer()

            MetaModel.cluster = LocalCluster(n_workers=16, processes=True)

            # MetaModel.cluster = FargateCluster(
            #     image="234659567514.dkr.ecr.us-west-2.amazonaws.com/hwpc-calc:test",
            #     scheduler_cpu=2048,
            #     scheduler_mem=4096,
            #     worker_cpu=1024,
            #     worker_nthreads=2,
            #     worker_mem=2048,
            #     n_workers=32,
            # )

            # MetaModel.cluster.adapt(minimum=32, maximum=72, wait_count=60, target_duration="100s")

            MetaModel.client = Client(MetaModel.cluster)

            MetaModel.lock = Lock("plock")

            print(MetaModel.client)

        return cls._instance

    def run_simulation(self):
        """ """
        sim_start = timeit.default_timer()
        md = model_data.ModelData(path=nm.Output.input_path)
        harvest = md.data[nm.Tables.harvest]

        years = harvest[nm.Fields.harvest_year]

        final_futures = model.Model.model_factory(model_data_path=nm.Output.input_path, harvest_init=harvest)
        ac = as_completed(final_futures)
        year_ds_col_all = dict()
        year_ds_col_rec = dict()

        ds_all = None
        ds_rec = None

        try:
            for f in ac:
                r, r_futures = f.result()

                ykey = r.lineage[0]
                if ykey == 2011:
                    j = 0
                if ykey in year_ds_col_all:
                    year_ds_col_all[ykey] = MetaModel.aggregate_results(year_ds_col_all[ykey], r)
                else:
                    year_ds_col_all[ykey] = r

                if ds_all is None:
                    ds_all = r
                else:
                    ds_all = MetaModel.aggregate_results(ds_all, r)

                # Save the recycled materials on their own for reporting
                if len(r.lineage) > 1:
                    if ykey in year_ds_col_rec:
                        year_ds_col_rec[ykey] = MetaModel.aggregate_results(year_ds_col_rec[ykey], r)
                    else:
                        year_ds_col_rec[ykey] = r

                    if ds_rec is None:
                        ds_rec = r
                    else:
                        ds_rec = MetaModel.aggregate_results(ds_rec, r)

                if r_futures:
                    ac.update(r_futures)

                f.release()  # This function is not actually documented, but it seems to be needed
                del f

            ds_all[nm.Fields.ccf] = harvest[nm.Fields.ccf]

            with Lock("plock"):
                print("===========================")
                print("Model run time", f"{(timeit.default_timer() - MetaModel.start) / 60} minutes")
                print("===========================")

            m = MetaModel.make_results(ds_all, save=True)
            if ds_rec is not None:
                m = MetaModel.make_results(ds_rec, save=True)
            for y in year_ds_col_all:
                if y == 2011:
                    n = 1
                m = MetaModel.make_results(year_ds_col_all[y], prefix=str(y), save=True)
                if ds_rec is not None:
                    m = MetaModel.make_results(year_ds_col_all[y], prefix=str(y) + "_rec", save=True)

            with Lock("plock"):
                print("===========================")
                print("Final run time", f"{(timeit.default_timer() - MetaModel.start) / 60} minutes")
                print("===========================")

        except Exception as e:
            MetaModel.cluster.close()
            print(e)
            traceback.print_exc()
        return

    @staticmethod
    def aggregate_results(src_ds, new_ds):
        if src_ds.lineage[-1] > new_ds.lineage[-1]:
            return MetaModel.aggregate_results(new_ds, src_ds)

        new_ds = new_ds.merge(src_ds, join="right", fill_value=0, compat="override")
        src_ds[nm.Fields.end_use_results] = src_ds[nm.Fields.end_use_results] + new_ds[nm.Fields.end_use_results]
        src_ds[nm.Fields.end_use_sum] = src_ds[nm.Fields.end_use_sum] + new_ds[nm.Fields.end_use_sum]
        src_ds[nm.Fields.products_in_use] = src_ds[nm.Fields.products_in_use] + new_ds[nm.Fields.products_in_use]
        src_ds[nm.Fields.discarded_products_results] = src_ds[nm.Fields.discarded_products_results] + new_ds[nm.Fields.discarded_products_results]
        src_ds[nm.Fields.discard_dispositions] = src_ds[nm.Fields.discard_dispositions] + new_ds[nm.Fields.discard_dispositions]
        src_ds[nm.Fields.can_decay] = src_ds[nm.Fields.can_decay] + new_ds[nm.Fields.can_decay]
        src_ds[nm.Fields.fixed] = src_ds[nm.Fields.fixed] + new_ds[nm.Fields.fixed]
        src_ds[nm.Fields.discard_remaining] = src_ds[nm.Fields.discard_remaining] + new_ds[nm.Fields.discard_remaining]
        src_ds[nm.Fields.could_decay] = src_ds[nm.Fields.could_decay] + new_ds[nm.Fields.could_decay]
        src_ds[nm.Fields.emitted] = src_ds[nm.Fields.emitted] + new_ds[nm.Fields.emitted]
        src_ds[nm.Fields.present] = src_ds[nm.Fields.present] + new_ds[nm.Fields.present]
        return src_ds

    @staticmethod
    def c_to_co2e(c: float) -> float:
        """Convert C to CO2e.

        Args:
            c (float): the C value to convert

        Returns:
            float: Units of CO2
        """
        return c * 44.0 / 12.0

    @staticmethod
    def make_results(ds, prefix="", save=False):

        C = nm.Fields.c
        MGC = nm.Fields.mgc
        CO2 = nm.Fields.co2
        P = nm.Fields.ppresent
        E = nm.Fields.eemitted
        CHANGE = nm.Fields.change

        if prefix == "2011":
            i = 3

        final_e = ds[[nm.Fields.end_use_results, nm.Fields.products_in_use, nm.Fields.discarded_products_results]].sum(dim="EndUseID")
        final_d = ds[
            [
                nm.Fields.discard_dispositions,
                nm.Fields.can_decay,
                nm.Fields.fixed,
                nm.Fields.discard_remaining,
                nm.Fields.could_decay,
                nm.Fields.emitted,
                nm.Fields.present,
            ]
        ].sum(dim=["EndUseID", "DiscardDestinationID"])
        final = xr.merge([final_e, final_d])

        annual_harvest_and_timber = ds[[nm.Fields.ccf, nm.Fields.end_use_results]].sum(dim=nm.Fields.end_use_id)
        annual_harvest_and_timber = annual_harvest_and_timber.rename_vars(
            {nm.Fields.ccf: C(nm.Fields.ccf), nm.Fields.end_use_results: MGC(nm.Fields.end_use_results)}
        )

        compost_emitted = ds[nm.Fields.emitted].loc[dict(DiscardDestinationID=2)].sum(dim=nm.Fields.end_use_id)
        compost_emitted = MetaModel.c_to_co2e(compost_emitted)
        compost_emitted.name = CO2(E(nm.Fields.composted))
        compost_emitted = compost_emitted.drop_vars(nm.Fields.discard_destination_id)
        # compost_emitted = compost_emitted.cumsum()

        carbon_present_landfills = ds[nm.Fields.present].loc[dict(DiscardDestinationID=3)].sum(dim=nm.Fields.end_use_id)
        carbon_present_landfills.name = MGC(P(nm.Fields.landfills))
        carbon_present_landfills = carbon_present_landfills.drop_vars(nm.Fields.discard_destination_id)
        carbon_emitted_landfills = ds[nm.Fields.emitted].loc[dict(DiscardDestinationID=3)].sum(dim=nm.Fields.end_use_id)
        carbon_emitted_landfills = MetaModel.c_to_co2e(carbon_emitted_landfills)
        carbon_emitted_landfills.name = CO2(E(nm.Fields.landfills))
        carbon_emitted_landfills = carbon_emitted_landfills.drop_vars(nm.Fields.discard_destination_id)

        carbon_present_dumps = ds[nm.Fields.present].loc[dict(DiscardDestinationID=4)].sum(dim=nm.Fields.end_use_id)
        carbon_present_dumps.name = MGC(P(nm.Fields.dumps))
        carbon_present_dumps = carbon_present_dumps.drop_vars(nm.Fields.discard_destination_id)
        carbon_emitted_dumps = ds[nm.Fields.emitted].loc[dict(DiscardDestinationID=4)].sum(dim=nm.Fields.end_use_id)
        carbon_emitted_dumps = MetaModel.c_to_co2e(carbon_emitted_dumps)
        carbon_emitted_dumps.name = CO2(E(nm.Fields.dumps))
        carbon_emitted_dumps = carbon_emitted_dumps.drop_vars(nm.Fields.discard_destination_id)

        # TODO do we need to carry over the PIU to Emitted for fuels?
        # fuel_carbon_emitted = ds[nm.Fields.products_in_use].where(ds.data_vars[nm.Fields.fuel] == 1, drop=True).sum(dim=nm.Fields.end_use_id) # old method, for backup
        fuel_carbon_emitted = (
            ds[nm.Fields.emitted].loc[dict(DiscardDestinationID=0)].where(ds.data_vars[nm.Fields.fuel] == 1, drop=True).sum(dim=nm.Fields.end_use_id)
        )
        fuel_carbon_emitted = MetaModel.c_to_co2e(fuel_carbon_emitted)
        fuel_carbon_emitted.name = CO2(E(nm.Fields.fuel))
        # fuel_carbon_emitted = fuel_carbon_emitted.cumsum()

        end_use_in_use = ds[nm.Fields.products_in_use].sum(dim=nm.Fields.end_use_id)
        end_use_in_use.name = MGC(nm.Fields.products_in_use)

        # TODO this is probably wrong (some should come from emitted probably...)
        burned_without_energy_capture = ds[nm.Fields.emitted].loc[dict(DiscardDestinationID=0)].sum(dim=nm.Fields.end_use_id)
        burned_without_energy_capture = MetaModel.c_to_co2e(burned_without_energy_capture)
        burned_without_energy_capture.name = CO2(E(nm.Fields.burned_wo_energy_capture))
        # burned_without_energy_capture_cum = burned_without_energy_capture.cumsum()
        # TODO discard_burned_with_energy_capture
        burned_with_energy_capture = fuel_carbon_emitted  # TODO + discard_burned_with_energy_capture

        carbon_present_swds = carbon_present_landfills + carbon_present_dumps
        carbon_present_swds.name = MGC(P(nm.Fields.present))

        cumulative_carbon_stocks = xr.Dataset({MGC(nm.Fields.products_in_use): end_use_in_use, MGC(nm.Fields.swds): carbon_present_swds})
        cumulative_carbon_stocks[MGC(CHANGE(nm.Fields.products_in_use))] = cumulative_carbon_stocks[MGC(nm.Fields.products_in_use)].diff(
            dim=nm.Fields.harvest_year
        )
        cumulative_carbon_stocks[MGC(CHANGE(nm.Fields.swds))] = cumulative_carbon_stocks[MGC(nm.Fields.swds)].diff(dim=nm.Fields.harvest_year)

        # totalYearlyNetChange PDF
        cumulative_stock_change = (
            cumulative_carbon_stocks[MGC(CHANGE(nm.Fields.swds))] + cumulative_carbon_stocks[MGC(CHANGE(nm.Fields.products_in_use))]
        )
        cumulative_stock_change.name = MGC(CHANGE(nm.Fields.present))
        # totalSelectedNetChange PDF
        cumulative_selected_stock_change = cumulative_carbon_stocks.sel(Year=cumulative_carbon_stocks[nm.Fields.harvest_year] % 5 == 0)

        emitted_w_ec = fuel_carbon_emitted
        emitted_w_ec.name = CO2(nm.Fields.emitted_with_energy_capture)
        emitted_wo_ec = compost_emitted + carbon_emitted_landfills + carbon_emitted_dumps + burned_without_energy_capture
        emitted_wo_ec.name = CO2(nm.Fields.emitted_wo_energy_capture)

        big_four = xr.Dataset(
            {
                CO2(P(nm.Fields.products_in_use)): MetaModel.c_to_co2e(end_use_in_use),
                CO2(P(nm.Fields.swds)): MetaModel.c_to_co2e(carbon_present_swds),
                CO2(E(nm.Fields.emitted_with_energy_capture)): emitted_w_ec,
                CO2(E(nm.Fields.emitted_wo_energy_capture)): emitted_wo_ec,
            }
        )
        big_four = big_four.squeeze()
        emitted_all = xr.Dataset(
            {
                CO2(E(nm.Fields.fuel)): fuel_carbon_emitted,
                CO2(E(nm.Fields.composted)): compost_emitted,
                CO2(E(nm.Fields.dumps)): carbon_emitted_dumps,
                CO2(E(nm.Fields.landfills)): carbon_emitted_landfills,
            }
        )
        emitted_all = emitted_all.squeeze()
        carbon_present_distinct_swds = xr.Dataset(
            {MGC(P(nm.Fields.dumps)): carbon_present_dumps, MGC(P(nm.Fields.landfills)): carbon_present_landfills}
        )
        carbon_emitted_distinct_swds = xr.Dataset(
            {CO2(E(nm.Fields.dumps)): carbon_emitted_dumps, CO2(E(nm.Fields.landfills)): carbon_emitted_landfills}
        )

        # totalYearlyDispositions PDF
        mega_table = xr.Dataset(
            {
                CO2(nm.Fields.emitted_with_energy_capture): emitted_w_ec,
                CO2(CHANGE(nm.Fields.emitted_with_energy_capture)): emitted_w_ec.diff(dim=nm.Fields.harvest_year),
                CO2(nm.Fields.emitted_wo_energy_capture): emitted_wo_ec,
                CO2(CHANGE(nm.Fields.emitted_wo_energy_capture)): emitted_wo_ec.diff(dim=nm.Fields.harvest_year),
                MGC(nm.Fields.products_in_use): cumulative_carbon_stocks[MGC(nm.Fields.products_in_use)],
                MGC(CHANGE(nm.Fields.products_in_use)): cumulative_carbon_stocks[MGC(CHANGE(nm.Fields.products_in_use))],
                MGC(nm.Fields.swds): cumulative_carbon_stocks[MGC(nm.Fields.swds)],
                MGC(CHANGE(nm.Fields.swds)): cumulative_carbon_stocks[MGC(CHANGE(nm.Fields.swds))],
                MGC(nm.Fields.present): cumulative_carbon_stocks[MGC(nm.Fields.swds)] + cumulative_carbon_stocks[MGC(nm.Fields.products_in_use)],
                MGC(CHANGE(nm.Fields.present)): cumulative_stock_change,
            }
        )

        # totalSelectedDispositions PDF
        mega_selected_table = mega_table.sel(Year=cumulative_carbon_stocks[nm.Fields.harvest_year] % 5 == 0)

        if save:
            zip_buffer = BytesIO()

            zip = zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_STORED, allowZip64=False)
            # with tempfile.TemporaryFile() as temp:
            # harvest_data.to_csv(temp)
            # temp.seek(0)
            # self.zip.writestr('harvest_data.csv', temp.read(), compress_type=zipfile.ZIP_STORED)
            if len(prefix) > 1:
                prefix = prefix + "_"
            with tempfile.TemporaryFile() as temp:
                ds.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "results.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                final.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "final.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                annual_harvest_and_timber.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "annual_harvest_and_timber_product_output.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                cumulative_carbon_stocks[[MGC(CHANGE(nm.Fields.products_in_use)), MGC(CHANGE(nm.Fields.swds))]].to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "annual_net_change_carbon_stocks.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                burned_without_energy_capture.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "burned_wo_energy_capture_emit.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                burned_with_energy_capture.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "burned_w_energy_capture_emit.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                compost_emitted.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "total_composted_carbon_emitted.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                cumulative_carbon_stocks[[MGC(nm.Fields.products_in_use), MGC(nm.Fields.swds)]].to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "total_cumulative_carbon_stocks.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                carbon_emitted_dumps.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "total_dumps_carbon_emitted.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                carbon_present_dumps.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "total_dumps_carbon.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                end_use_in_use.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "total_end_use_products.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                fuel_carbon_emitted.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "total_fuelwood_carbon_emitted.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                carbon_emitted_landfills.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "total_landfills_carbon_emitted.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                carbon_present_landfills.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "total_landfills_carbon.csv", temp.read(), compress_type=zipfile.ZIP_STORED)

            # Flashy page outputs
            with tempfile.TemporaryFile() as temp:
                big_four.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "big_four.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                emitted_all.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "emitted_all.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                carbon_present_distinct_swds.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "carbon_present_distinct_swds.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                carbon_emitted_distinct_swds.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "carbon_emitted_distinct_swds.csv", temp.read(), compress_type=zipfile.ZIP_STORED)

            # PDF table outputs
            with tempfile.TemporaryFile() as temp:
                cumulative_stock_change.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "total_yearly_net_change.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                cumulative_selected_stock_change.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "total_selected_net_change.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                mega_table.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "total_yearly_dispositions.csv", temp.read(), compress_type=zipfile.ZIP_STORED)
            with tempfile.TemporaryFile() as temp:
                mega_selected_table.to_dataframe().to_csv(temp)
                temp.seek(0)
                zip.writestr(prefix + "total_selected_dispositions.csv", temp.read(), compress_type=zipfile.ZIP_STORED)

            zip.close()
            zip_buffer.seek(0)
            S3Helper.upload_file(zip_buffer, "hwpc-output", nm.Output.output_path + "/results/" + prefix + nm.Output.run_name + ".zip")

        return
