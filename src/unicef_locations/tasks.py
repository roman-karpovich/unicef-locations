import time

import celery
from carto.exceptions import CartoException
from carto.sql import SQLClient
from celery.utils.log import get_task_logger

from django.db import IntegrityError, transaction
from django.utils.encoding import force_text

from .auth import LocationsCartoNoAuthClient
from .models import CartoDBTable, Location

logger = get_task_logger(__name__)


def create_location(pcode, carto_table, parent, parent_instance,
                    remapped_old_pcodes, site_name, row, sites_not_added,
                    sites_created, sites_updated, sites_remapped):
    results = None

    try:
        location = None
        remapped_locations = None
        if remapped_old_pcodes:
            # check if the remapped location exists in the database
            remapped_locations = Location.objects.filter(p_code__in=list(remapped_old_pcodes))

            if not remapped_locations:
                # if remapped_old_pcodes are set and passed validations, but they are not found in the
                # list of the active locations(`Location.objects`), it means that they were already remapped.
                # in this case update the `main` location, and ignore the remap.
                location = Location.objects.get(p_code=pcode)
        else:
            location = Location.objects.get(p_code=pcode)

    except Location.MultipleObjectsReturned:
        logger.warning("Multiple locations found for: {}, {} ({})".format(
            carto_table.location_type, site_name, pcode
        ))
        sites_not_added += 1
        return False, sites_not_added, sites_created, sites_updated, sites_remapped, results

    except Location.DoesNotExist:
        pass

    if not location:
        # try to create the location
        create_args = {
            'p_code': pcode,
            'gateway': carto_table.location_type,
            'name': site_name
        }
        if parent and parent_instance:
            create_args['parent'] = parent_instance

        if not row['the_geom']:
            sites_not_added += 1
            return False, sites_not_added, sites_created, sites_updated, sites_remapped, results

        if 'Point' in row['the_geom']:
            create_args['point'] = row['the_geom']
        else:
            create_args['geom'] = row['the_geom']

        try:
            location = Location.objects.create(**create_args)
            sites_created += 1
        except IntegrityError:
            logger.exception('Error while creating location: %s', site_name)
            sites_not_added += 1
            return False, sites_not_added, sites_created, sites_updated, sites_remapped, results

        logger.info('{}: {} ({})'.format(
            'Added',
            location.name,
            carto_table.location_type.name
        ))

        results = []
        if remapped_locations:
            for remapped_location in remapped_locations:
                remapped_location.is_active = False
                # remapped_location.parent = None
                remapped_location.save()

                sites_remapped += 1
                logger.info('{}: {} ({})'.format(
                    'Remapped',
                    remapped_location.name,
                    carto_table.location_type.name
                ))

                results.append((location.id, remapped_location.id))
        else:
            results = [(location.id, None)]

        return True, sites_not_added, sites_created, sites_updated, sites_remapped, results

    else:
        if not row['the_geom']:
            return False, sites_not_added, sites_created, sites_updated, sites_remapped, results

        # names can be updated for existing locations with the same code
        location.name = site_name

        if 'Point' in row['the_geom']:
            location.point = row['the_geom']
        else:
            location.geom = row['the_geom']

        if parent and parent_instance:
            logger.info("Updating parent:{} for location {}".format(parent_instance, location))
            location.parent = parent_instance
        else:
            location.parent = None

        try:
            location.save()
        except IntegrityError:
            logger.exception('Error while saving location: %s', site_name)
            return False, sites_not_added, sites_created, sites_updated, sites_remapped, results

        sites_updated += 1
        logger.info('{}: {} ({})'.format(
            'Updated',
            location.name,
            carto_table.location_type.name
        ))

        results = [(location.id, None)]
        return True, sites_not_added, sites_created, sites_updated, sites_remapped, results


@celery.current_app.task
def update_sites_from_cartodb(carto_table_pk):
    results = []

    try:
        carto_table = CartoDBTable.objects.get(pk=carto_table_pk)
    except CartoDBTable.DoesNotExist:
        logger.exception('Cannot retrieve CartoDBTable with pk: %s', carto_table_pk)
        return

    auth_client = LocationsCartoNoAuthClient(base_url="https://{}.carto.com/".format(carto_table.domain))
    sql_client = SQLClient(auth_client)
    sites_created = sites_updated = sites_remapped = sites_not_added = 0

    # query for cartodb
    qry = ''
    rows = []
    cartodb_id_col = 'cartodb_id'

    try:
        query_row_count = sql_client.send('select count(*) from {}'.format(carto_table.table_name))
        row_count = query_row_count['rows'][0]['count']

        # do not spam Carto with requests, wait 1 second
        time.sleep(1)
        query_max_id = sql_client.send('select MAX({}) from {}'.format(cartodb_id_col, carto_table.table_name))
        max_id = query_max_id['rows'][0]['max']
    except CartoException:
        logger.exception("Cannot fetch pagination prequisites from CartoDB for table {}".format(
            carto_table.table_name
        ))
        return "Table name {}: {} sites created, {} sites updated, {} sites remapped, {} sites skipped".format(
            carto_table.table_name, 0, 0, 0, 0
        )

    offset = 0
    limit = 100

    # failsafe in the case when cartodb id's are too much off compared to the nr. of records
    if max_id > (5 * row_count):
        limit = max_id + 1
        logger.warning("The CartoDB primary key seemf off, pagination is not possible")

    if carto_table.parent_code_col and carto_table.parent:
        qry = 'select st_AsGeoJSON(the_geom) as the_geom, {}, {}, {} from {}'.format(
            carto_table.name_col,
            carto_table.pcode_col,
            carto_table.parent_code_col,
            carto_table.table_name)
    else:
        qry = 'select st_AsGeoJSON(the_geom) as the_geom, {}, {} from {}'.format(
            carto_table.name_col,
            carto_table.pcode_col,
            carto_table.table_name)

    try:
        while offset <= max_id:
            paged_qry = qry + ' WHERE {} > {} AND {} <= {}'.format(
                cartodb_id_col,
                offset,
                cartodb_id_col,
                offset + limit
            )
            logger.info('Requesting rows between {} and {} for {}'.format(
                offset,
                offset + limit,
                carto_table.table_name
            ))

            # do not spam Carto with requests, wait 1 second
            time.sleep(1)
            sites = sql_client.send(paged_qry)
            rows += sites['rows']
            offset += limit

            if 'error' in sites:
                # it seems we can have both valid results and error messages in the same CartoDB response
                logger.exception("CartoDB API error received: {}".format(sites['error']))
                # When this error occurs, we receive truncated locations, probably it's better to interrupt the import
                return
    except CartoException:
        logger.exception("CartoDB exception occured")
    else:
        # validations
        # get the list of the existing Pcodes and previous Pcodes from the database

        # New locations must have unique Pcodes (within a given admin level)
        # Old locations must have unique Pcodes (within a given admin level)
        # Locations case B in use (BiU) must be provided with remaps in the remap table - if not - cancel import
        # Locations case BnRiU are not allowed
        # Old Pcodes provided in the remap table must be unique (can't be provided multiple times)
        # New Pcodes provided in the remap table must exist in the new dataset (Carto table)
        # New locations must have correct geometry
        # Upon successfully completed update the total number of "Active" locations per admin level equals the number of locations in New dataset (Carto db table)
        # Total number of locations marked as "Inactive" equals the number of records in remap table
        # reset to root level if:
        #   - outsider
        #   - gets archived
        #   - archived lower level locations whose parents are deleted go to root level
        #   -
        # update children parents on remap
        # also filter api by 'is_active'
        # put celery tasks in chain


        database_pcodes = []
        for row in Location.all_locations.filter(gateway=carto_table.location_type).values('p_code'):
            database_pcodes.append(row['p_code'])

        # get the list of the new Pcodes from the Carto data
        new_carto_pcodes = [str(row[carto_table.pcode_col]) for row in rows]

        # print(database_pcodes)
        # print(new_carto_pcodes)
        remap_old_pcodes = []
        remap_new_pcodes = []

        # if we have a remap table, fetch it's content and validate it
        if carto_table.remap_table_name:
            remapped_pcode_pairs = []
            try:
                remap_qry = 'select old_pcode::text, new_pcode::text from {}'.format(
                    carto_table.remap_table_name)
                remapped_pcode_pairs = sql_client.send(remap_qry)['rows']
            except CartoException:
                logger.exception("CartoDB exception occured on the remap table query")
                return
            else:
                validation_failed = False
                # test
                # remapped_pcode_pairs.append({'new_pcode': 'testn1', 'old_pcode': 'testo1'})
                # remapped_pcode_pairs.append({'new_pcode': 'testn2', 'old_pcode': 'testo2'})

                # validate remap table
                bad_old_pcodes = []
                bad_new_pcodes = []
                for remap_row in remapped_pcode_pairs:
                    remap_old_pcodes.append(remap_row['old_pcode'])
                    remap_new_pcodes.append(remap_row['new_pcode'])

                    # check for non-existing remap pcodes in the database
                    if remap_row['old_pcode'] not in database_pcodes:
                        bad_old_pcodes.append(remap_row['old_pcode'])
                    # check for non-existing remap pcodes in the Carto dataset
                    if remap_row['new_pcode'] not in new_carto_pcodes:
                        bad_new_pcodes.append(remap_row['new_pcode'])

                if len(bad_old_pcodes) > 0:
                    logger.exception(
                        "Invalid old_pcode found in the remap table: {}".format(','.join(bad_old_pcodes)))
                    validation_failed = True

                if len(bad_new_pcodes) > 0:
                    logger.exception(
                        "Invalid new_pcode found in the remap table: {}".format(','.join(bad_new_pcodes)))
                    validation_failed = True

                if validation_failed is True:
                    return

        # find duplicate pcodes in both local and Carto data
        duplicates_found = False
        temp = {}
        duplicate_database_pcodes = []
        for database_pcode in database_pcodes:
            if database_pcode in temp:
                duplicate_database_pcodes.append(database_pcode)
            temp[database_pcode] = 1

        if duplicate_database_pcodes:
            logger.exception("Duplicates found in the existing database pcodes: {}".
                             format(','.join(duplicate_database_pcodes)))
            duplicates_found = True

        temp = {}
        duplicate_carto_pcodes = []
        for new_carto_pcode in new_carto_pcodes:
            if new_carto_pcode in temp:
                duplicate_carto_pcodes.append(new_carto_pcode)
            temp[new_carto_pcode] = 1

        if duplicate_carto_pcodes:
            logger.exception("Duplicates found in the new CartoDB pcodes: {}".
                             format(','.join(duplicate_database_pcodes)))
            duplicates_found = True

        temp = {}
        duplicate_remap_old_pcodes = []
        for remap_old_pcode in remap_old_pcodes:
            if remap_old_pcode in temp:
                duplicate_remap_old_pcodes.append(remap_old_pcode)
            temp[remap_old_pcode] = 1

        if duplicate_remap_old_pcodes:
            logger.exception("Duplicates found in the remap table `old pcode` column: {}".
                             format(','.join(duplicate_remap_old_pcodes)))
            duplicates_found = True

        if duplicates_found:
            return

        orphaned_old_pcodes = set(database_pcodes) - (set(new_carto_pcodes) | set(remap_old_pcodes))
        # TODO: check for in use, in etools.src.libraries

        # wrap Location tree updates in a transaction, to prevent an invalid tree state due to errors
        with transaction.atomic():
            # disable tree 'generation' during single row updates, rebuild the tree after.
            # this should prevent errors happening (probably)due to invalid intermediary tree state
            with Location.objects.disable_mptt_updates():
                for row in rows:
                    carto_pcode = str(row[carto_table.pcode_col]).strip()
                    site_name = row[carto_table.name_col]

                    if not site_name or site_name.isspace():
                        logger.warning("No name for location with PCode: {}".format(carto_pcode))
                        sites_not_added += 1
                        continue

                    parent = None
                    parent_code = None
                    parent_instance = None

                    # attempt to reference the parent of this location
                    if carto_table.parent_code_col and carto_table.parent:
                        msg = None
                        parent = carto_table.parent.__class__
                        parent_code = row[carto_table.parent_code_col]
                        try:
                            parent_instance = Location.objects.get(p_code=parent_code)
                        except Location.MultipleObjectsReturned:
                            msg = "Multiple locations found for parent code: {}".format(
                                parent_code
                            )
                        except Location.DoesNotExist:
                            msg = "No locations found for parent code: {}".format(
                                parent_code
                            )
                        except Exception as exp:
                            msg = force_text(exp)

                        if msg is not None:
                            logger.warning(msg)
                            sites_not_added += 1
                            continue

                    # check if the Carto location should be remapped to an old location
                    remapped_old_pcodes = set()
                    if carto_table.remap_table_name and len(remapped_pcode_pairs) > 0:
                        for remap_row in remapped_pcode_pairs:
                            if carto_pcode == remap_row['new_pcode']:
                                remapped_old_pcodes.add(remap_row['old_pcode'])

                    # create the location or update the existing based on type and code
                    succ, sites_not_added, sites_created, sites_updated, sites_remapped, \
                    partial_results = create_location(
                        carto_pcode, carto_table,
                        parent, parent_instance, remapped_old_pcodes,
                        site_name, row,
                        sites_not_added, sites_created,
                        sites_updated, sites_remapped
                    )

                    results += partial_results

                if orphaned_old_pcodes:
                    logger.warning("Archiving unused pcodes: {}".format(','.join(orphaned_old_pcodes)))
                    # Location.objects.filter(p_code__in=list(orphaned_old_pcodes)).delete()
                    Location.objects.filter(p_code__in=list(orphaned_old_pcodes)).update(is_active=False)

            Location.objects.rebuild()

    logger.warning("Table name {}: {} sites created, {} sites updated, {} sites remapped, {} sites skipped".format(
        carto_table.table_name, sites_created, sites_updated, sites_remapped, sites_not_added))

    return results
