# Copyright 2015 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
import time

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import excutils
from sqlalchemy.orm import exc as sa_exception
from taskflow import task
from taskflow.types import failure
import tenacity

from octavia.common import constants
from octavia.common import data_models
from octavia.common import exceptions
from octavia.common import utils
from octavia.controller.worker import task_utils
from octavia.db import api as db_apis
from octavia.db import repositories as repo
from octavia.i18n import _
from octavia.network import base
from octavia.network import data_models as n_data_models

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class BaseNetworkTask(task.Task):
    """Base task to load drivers common to the tasks."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._network_driver = None
        self.task_utils = task_utils.TaskUtils()
        self.loadbalancer_repo = repo.LoadBalancerRepository()
        self.amphora_repo = repo.AmphoraRepository()
        self.amphora_member_port_repo = repo.AmphoraMemberPortRepository()

    @property
    def network_driver(self):
        if self._network_driver is None:
            self._network_driver = utils.get_network_driver()
        return self._network_driver


class CalculateAmphoraDelta(BaseNetworkTask):

    default_provides = constants.DELTA

    def execute(self, loadbalancer, amphora, availability_zone):
        LOG.debug("Calculating network delta for amphora id: %s",
                  amphora.get(constants.ID))

        # Figure out what networks we want
        # seed with lb network(s)
        if (availability_zone and
                availability_zone.get(constants.MANAGEMENT_NETWORK)):
            management_nets = [
                availability_zone.get(constants.MANAGEMENT_NETWORK)]
        else:
            management_nets = CONF.controller_worker.amp_boot_network_list

        session = db_apis.get_session()
        with session.begin():
            db_lb = self.loadbalancer_repo.get(
                session, id=loadbalancer[constants.LOADBALANCER_ID])

        desired_subnet_to_net_map = {
            loadbalancer[constants.VIP_SUBNET_ID]:
            loadbalancer[constants.VIP_NETWORK_ID]
        }

        net_vnic_type_map = {}
        for pool in db_lb.pools:
            for member in pool.members:
                if (member.subnet_id and
                        member.provisioning_status !=
                        constants.PENDING_DELETE):
                    member_network = self.network_driver.get_subnet(
                        member.subnet_id).network_id
                    net_vnic_type_map[member_network] = getattr(
                        member, 'vnic_type', constants.VNIC_TYPE_NORMAL)
                    desired_subnet_to_net_map[member.subnet_id] = (
                        member_network)

        desired_network_ids = set(desired_subnet_to_net_map.values())
        desired_subnet_ids = set(desired_subnet_to_net_map)

        # Calculate Network deltas
        nics = self.network_driver.get_plugged_networks(
            amphora[constants.COMPUTE_ID])
        # we don't have two nics in the same network
        # Don't include the nics connected to the management network, we don't
        # want to update these interfaces.
        network_to_nic_map = {
            nic.network_id: nic
            for nic in nics
            if nic.network_id not in management_nets}

        plugged_network_ids = set(network_to_nic_map)

        del_ids = plugged_network_ids - desired_network_ids
        delete_nics = [n_data_models.Interface(
            network_id=net_id,
            port_id=network_to_nic_map[net_id].port_id)
            for net_id in del_ids]

        add_ids = desired_network_ids - plugged_network_ids
        add_nics = [n_data_models.Interface(
            network_id=add_net_id,
            fixed_ips=[
                n_data_models.FixedIP(
                    subnet_id=subnet_id)
                for subnet_id, net_id in desired_subnet_to_net_map.items()
                if net_id == add_net_id],
            vnic_type=net_vnic_type_map[add_net_id])
            for add_net_id in add_ids]

        # Calculate member Subnet deltas
        plugged_subnets = {}
        for nic in network_to_nic_map.values():
            for fixed_ip in nic.fixed_ips or []:
                plugged_subnets[fixed_ip.subnet_id] = nic.network_id

        plugged_subnet_ids = set(plugged_subnets)
        del_subnet_ids = plugged_subnet_ids - desired_subnet_ids
        add_subnet_ids = desired_subnet_ids - plugged_subnet_ids

        def _subnet_updates(subnet_ids, subnets):
            updates = []
            for s in subnet_ids:
                network_id = subnets[s]
                nic = network_to_nic_map.get(network_id)
                port_id = nic.port_id if nic else None
                updates.append({
                    constants.SUBNET_ID: s,
                    constants.NETWORK_ID: network_id,
                    constants.PORT_ID: port_id
                })
            return updates

        add_subnets = _subnet_updates(add_subnet_ids,
                                      desired_subnet_to_net_map)
        del_subnets = _subnet_updates(del_subnet_ids,
                                      plugged_subnets)

        delta = n_data_models.Delta(
            amphora_id=amphora[constants.ID],
            compute_id=amphora[constants.COMPUTE_ID],
            add_nics=add_nics, delete_nics=delete_nics,
            add_subnets=add_subnets,
            delete_subnets=del_subnets)
        return delta.to_dict(recurse=True)


class CalculateDelta(BaseNetworkTask):
    """Task to calculate the delta between

    the nics on the amphora and the ones
    we need. Returns a list for
    plumbing them.
    """

    default_provides = constants.DELTAS

    def execute(self, loadbalancer, availability_zone):
        """Compute which NICs need to be plugged

        for the amphora to become operational.

        :param loadbalancer: the loadbalancer to calculate deltas for all
                             amphorae
        :param availability_zone: availability zone metadata dict

        :returns: dict of octavia.network.data_models.Delta keyed off amphora
                  id
        """

        calculate_amp = CalculateAmphoraDelta()
        deltas = {}
        session = db_apis.get_session()
        with session.begin():
            db_lb = self.loadbalancer_repo.get(
                session, id=loadbalancer[constants.LOADBALANCER_ID])
        for amphora in filter(
            lambda amp: amp.status == constants.AMPHORA_ALLOCATED,
                db_lb.amphorae):

            delta = calculate_amp.execute(loadbalancer, amphora.to_dict(),
                                          availability_zone)
            deltas[amphora.id] = delta
        return deltas


class GetPlumbedNetworks(BaseNetworkTask):
    """Task to figure out the NICS on an amphora.

    This will likely move into the amphora driver
    :returns: Array of networks
    """

    default_provides = constants.NICS

    def execute(self, amphora):
        """Get plumbed networks for the amphora."""

        LOG.debug("Getting plumbed networks for amphora id: %s",
                  amphora[constants.ID])

        return self.network_driver.get_plugged_networks(
            amphora[constants.COMPUTE_ID])


class UnPlugNetworks(BaseNetworkTask):
    """Task to unplug the networks

    Loop over all nics and unplug them
    based on delta
    """

    def execute(self, amphora, delta):
        """Unplug the networks."""

        LOG.debug("Unplug network for amphora")
        if not delta:
            LOG.debug("No network deltas for amphora id: %s",
                      amphora[constants.ID])
            return

        for nic in delta[constants.DELETE_NICS]:
            try:
                self.network_driver.unplug_network(
                    amphora[constants.COMPUTE_ID], nic[constants.NETWORK_ID])
            except base.NetworkNotFound:
                LOG.debug("Network %d not found", nic[constants.NETWORK_ID])
            except Exception:
                LOG.exception("Unable to unplug network")
                # TODO(xgerman) follow up if that makes sense


class GetMemberPorts(BaseNetworkTask):

    def execute(self, loadbalancer, amphora):
        vip_port = self.network_driver.get_port(loadbalancer['vip_port_id'])
        member_ports = []
        interfaces = self.network_driver.get_plugged_networks(
            amphora[constants.COMPUTE_ID])
        for interface in interfaces:
            port = self.network_driver.get_port(interface.port_id)
            if vip_port.network_id == port.network_id:
                continue
            port.network = self.network_driver.get_network(port.network_id)
            for fixed_ip in port.fixed_ips:
                if amphora['lb_network_ip'] == fixed_ip.ip_address:
                    break
                fixed_ip.subnet = self.network_driver.get_subnet(
                    fixed_ip.subnet_id)
            # Only add the port to the list if the IP wasn't the mgmt IP
            else:
                member_ports.append(port)
        return member_ports


class HandleNetworkDelta(BaseNetworkTask):
    """Task to plug and unplug networks

    Plug or unplug networks based on delta
    """

    def _fill_port_info(self, port):
        port.network = self.network_driver.get_network(port.network_id)
        for fixed_ip in port.fixed_ips:
            fixed_ip.subnet = self.network_driver.get_subnet(
                fixed_ip.subnet_id)

    def _cleanup_port(self, port_id, compute_id):
        try:
            self.network_driver.delete_port(port_id)
        except Exception:
            LOG.error(f'Unable to delete port {port_id} after failing to plug '
                      f'the port into compute {compute_id}. This port '
                      f'may now be abandoned in neutron.')

    def execute(self, amphora, delta):
        """Handle network plugging based off deltas."""
        session = db_apis.get_session()
        with session.begin():
            db_amp = self.amphora_repo.get(session,
                                           id=amphora.get(constants.ID))
        updated_ports = {}
        for nic in delta[constants.ADD_NICS]:
            network_id = nic[constants.NETWORK_ID]
            subnet_id = nic[constants.FIXED_IPS][0][constants.SUBNET_ID]

            try:
                port = self.network_driver.create_port(
                    network_id,
                    name=f'octavia-lb-member-{amphora.get(constants.ID)}',
                    vnic_type=nic[constants.VNIC_TYPE])
            except exceptions.NotFound as e:
                if 'Network' in str(e):
                    raise base.NetworkNotFound(str(e))
                raise base.CreatePortException(str(e))
            except Exception as e:
                message = _('Error creating a port on network {network_id}.'
                            ).format(network_id=network_id)
                LOG.exception(message)
                raise base.CreatePortException(message) from e

            try:
                self.network_driver.plug_port(db_amp, port)
            except exceptions.NotFound as e:
                self._cleanup_port(port.id, db_amp.compute_id)
                if 'Instance' in str(e):
                    raise base.AmphoraNotFound(str(e))
                raise base.PlugNetworkException(str(e))
            except Exception as e:
                self._cleanup_port(port.id, db_amp.compute_id)
                message = _('Error plugging amphora (compute_id: '
                            '{compute_id}) into network {network_id}.').format(
                    compute_id=db_amp.compute_id, network_id=network_id)
                LOG.exception(message)
                raise base.PlugNetworkException(message) from e
            with session.begin():
                self.amphora_member_port_repo.create(
                    session, port_id=port.id,
                    amphora_id=amphora.get(constants.ID),
                    network_id=network_id)

            self._fill_port_info(port)
            updated_ports[port.network_id] = port.to_dict(recurse=True)

        for update in delta.get(constants.ADD_SUBNETS, []):
            network_id = update[constants.NETWORK_ID]
            # Get already existing port from Deltas or
            # newly created port from updated_ports dict
            port_id = (update[constants.PORT_ID] or
                       updated_ports[network_id][constants.ID])
            subnet_id = update[constants.SUBNET_ID]
            # Avoid duplicated subnets
            has_subnet = False
            if network_id in updated_ports:
                has_subnet = any(
                    fixed_ip[constants.SUBNET_ID] == subnet_id
                    for fixed_ip in updated_ports[network_id][
                        constants.FIXED_IPS])
            if not has_subnet:
                port = self.network_driver.plug_fixed_ip(
                    port_id=port_id, subnet_id=subnet_id)
                self._fill_port_info(port)
                updated_ports[network_id] = (
                    port.to_dict(recurse=True))

        for update in delta.get(constants.DELETE_SUBNETS, []):
            network_id = update[constants.NETWORK_ID]
            port_id = update[constants.PORT_ID]
            subnet_id = update[constants.SUBNET_ID]
            port = self.network_driver.unplug_fixed_ip(
                port_id=port_id, subnet_id=subnet_id)
            self._fill_port_info(port)
            # In neutron, when removing an ipv6 subnet (with slaac) from a
            # port, it just ignores it.
            # https://bugs.launchpad.net/neutron/+bug/1945156
            # When it happens, don't add the port to the updated_ports dict
            has_subnet = any(
                fixed_ip.subnet_id == subnet_id
                for fixed_ip in port.fixed_ips)
            if not has_subnet:
                updated_ports[network_id] = (
                    port.to_dict(recurse=True))

        for nic in delta[constants.DELETE_NICS]:
            network_id = nic[constants.NETWORK_ID]
            try:
                self.network_driver.unplug_network(
                    db_amp.compute_id, network_id)
            except base.NetworkNotFound:
                LOG.debug("Network %s not found", network_id)
            except Exception:
                LOG.exception("Unable to unplug network")

            port_id = nic[constants.PORT_ID]
            try:
                self.network_driver.delete_port(port_id)
            except Exception:
                LOG.exception("Unable to delete the port")
            try:
                with session.begin():
                    self.amphora_member_port_repo.delete(session,
                                                         port_id=port_id)
            except sa_exception.NoResultFound:
                # Passively fail here for upgrade compatibility
                LOG.warning("No Amphora member port records found for "
                            "port_id: %s", port_id)

            updated_ports.pop(network_id, None)
        return {amphora[constants.ID]: list(updated_ports.values())}

    def revert(self, result, amphora, delta, *args, **kwargs):
        """Handle a network plug or unplug failures."""

        if isinstance(result, failure.Failure):
            return

        if not delta:
            return

        LOG.warning("Unable to plug networks for amp id %s",
                    delta['amphora_id'])

        for nic in delta[constants.ADD_NICS]:
            try:
                self.network_driver.unplug_network(delta[constants.COMPUTE_ID],
                                                   nic[constants.NETWORK_ID])
            except Exception:
                LOG.exception("Unable to unplug network %s",
                              nic[constants.NETWORK_ID])

            port_id = nic[constants.PORT_ID]
            try:
                self.network_driver.delete_port(port_id)
            except Exception:
                LOG.exception("Unable to delete port %s", port_id)


class HandleNetworkDeltas(BaseNetworkTask):
    """Task to plug and unplug networks

    Loop through the deltas and plug or unplug
    networks based on delta
    """

    def execute(self, deltas, loadbalancer):
        """Handle network plugging based off deltas."""
        session = db_apis.get_session()
        with session.begin():
            db_lb = self.loadbalancer_repo.get(
                session, id=loadbalancer[constants.LOADBALANCER_ID])
        amphorae = {amp.id: amp for amp in db_lb.amphorae}

        updated_ports = {}
        handle_delta = HandleNetworkDelta()

        for amp_id, delta in deltas.items():
            ret = handle_delta.execute(amphorae[amp_id].to_dict(), delta)
            updated_ports.update(ret)

        return updated_ports

    def revert(self, result, deltas, *args, **kwargs):
        """Handle a network plug or unplug failures."""

        if isinstance(result, failure.Failure):
            return

        if not deltas:
            return

        for amp_id, delta in deltas.items():
            LOG.warning("Unable to plug networks for amp id %s",
                        delta[constants.AMPHORA_ID])
            for nic in delta[constants.ADD_NICS]:
                try:
                    self.network_driver.unplug_network(
                        delta[constants.COMPUTE_ID],
                        nic[constants.NETWORK_ID])
                except Exception:
                    LOG.exception("Unable to unplug network %s",
                                  nic[constants.NETWORK_ID])

                port_id = nic[constants.PORT_ID]
                try:
                    self.network_driver.delete_port(port_id)
                except Exception:
                    LOG.exception("Unable to delete port %s", port_id)


class UpdateVIPSecurityGroup(BaseNetworkTask):
    """Task to setup SG for LB."""

    def execute(self, loadbalancer_id):
        """Task to setup SG for LB."""

        LOG.debug("Setting up VIP SG for load balancer id: %s",
                  loadbalancer_id)
        session = db_apis.get_session()
        with session.begin():
            db_lb = self.loadbalancer_repo.get(
                session, id=loadbalancer_id)

        sg_id = self.network_driver.update_vip_sg(db_lb, db_lb.vip)
        LOG.info("Set up VIP SG %s for load balancer %s complete",
                 sg_id if sg_id else "None", loadbalancer_id)
        return sg_id


class UpdateAmphoraSecurityGroup(BaseNetworkTask):
    """Task to update SGs for an Amphora."""

    def execute(self, loadbalancer_id: str):
        session = db_apis.get_session()
        with session.begin():
            db_lb = self.loadbalancer_repo.get(
                session, id=loadbalancer_id)
        for amp in db_lb.amphorae:
            self.network_driver.update_aap_port_sg(db_lb,
                                                   amp,
                                                   db_lb.vip)


class GetSubnetFromVIP(BaseNetworkTask):
    """Task to plumb a VIP."""

    def execute(self, loadbalancer):
        """Plumb a vip to an amphora."""

        LOG.debug("Getting subnet for LB: %s",
                  loadbalancer[constants.LOADBALANCER_ID])

        subnet = self.network_driver.get_subnet(loadbalancer['vip_subnet_id'])
        LOG.info("Got subnet %s for load balancer %s",
                 loadbalancer['vip_subnet_id'] if subnet else "None",
                 loadbalancer[constants.LOADBALANCER_ID])
        return subnet.to_dict()


class PlugVIPAmphora(BaseNetworkTask):
    """Task to plumb a VIP."""

    def execute(self, loadbalancer, amphora, subnet):
        """Plumb a vip to an amphora."""

        LOG.debug("Plumbing VIP for amphora id: %s",
                  amphora.get(constants.ID))
        session = db_apis.get_session()
        with session.begin():
            db_amp = self.amphora_repo.get(session,
                                           id=amphora.get(constants.ID))
            db_subnet = self.network_driver.get_subnet(subnet[constants.ID])
            db_lb = self.loadbalancer_repo.get(
                session, id=loadbalancer[constants.LOADBALANCER_ID])
        amp_data = self.network_driver.plug_aap_port(
            db_lb, db_lb.vip, db_amp, db_subnet)
        return amp_data.to_dict()

    def revert(self, result, loadbalancer, amphora, subnet, *args, **kwargs):
        """Handle a failure to plumb a vip."""
        if isinstance(result, failure.Failure):
            return
        lb_id = loadbalancer[constants.LOADBALANCER_ID]
        LOG.warning("Unable to plug VIP for amphora id %s "
                    "load balancer id %s",
                    amphora.get(constants.ID), lb_id)
        try:
            session = db_apis.get_session()
            with session.begin():
                db_amp = self.amphora_repo.get(session,
                                               id=amphora.get(constants.ID))
                db_amp.vrrp_port_id = result[constants.VRRP_PORT_ID]
                db_amp.ha_port_id = result[constants.HA_PORT_ID]
                db_subnet = self.network_driver.get_subnet(
                    subnet[constants.ID])
                db_lb = self.loadbalancer_repo.get(session, id=lb_id)
            self.network_driver.unplug_aap_port(db_lb.vip,
                                                db_amp, db_subnet)
        except Exception as e:
            LOG.error(
                'Failed to unplug AAP port for load balancer: %s. '
                'Resources may still be in use for VRRP port: %s. '
                'Due to error: %s',
                lb_id, result[constants.VRRP_PORT_ID], str(e)
            )


class UnplugVIP(BaseNetworkTask):
    """Task to unplug the vip."""

    def execute(self, loadbalancer):
        """Unplug the vip."""

        LOG.debug("Unplug vip on amphora")
        try:
            session = db_apis.get_session()
            with session.begin():
                db_lb = self.loadbalancer_repo.get(
                    session,
                    id=loadbalancer[constants.LOADBALANCER_ID])
            self.network_driver.unplug_vip(db_lb, db_lb.vip)
        except Exception:
            LOG.exception("Unable to unplug vip from load balancer %s",
                          loadbalancer[constants.LOADBALANCER_ID])


class AllocateVIP(BaseNetworkTask):
    """Task to allocate a VIP."""

    def execute(self, loadbalancer):
        """Allocate a vip to the loadbalancer."""

        LOG.debug("Allocating vip with port id %s, subnet id %s, "
                  "ip address %s for load balancer %s",
                  loadbalancer[constants.VIP_PORT_ID],
                  loadbalancer[constants.VIP_SUBNET_ID],
                  loadbalancer[constants.VIP_ADDRESS],
                  loadbalancer[constants.LOADBALANCER_ID])
        session = db_apis.get_session()
        with session.begin():
            db_lb = self.loadbalancer_repo.get(
                session, id=loadbalancer[constants.LOADBALANCER_ID])
        vip, additional_vips = self.network_driver.allocate_vip(db_lb)
        LOG.info("Allocated vip with port id %s, subnet id %s, ip address %s "
                 "for load balancer %s",
                 loadbalancer[constants.VIP_PORT_ID],
                 loadbalancer[constants.VIP_SUBNET_ID],
                 loadbalancer[constants.VIP_ADDRESS],
                 loadbalancer[constants.LOADBALANCER_ID])
        for add_vip in additional_vips:
            LOG.debug('Allocated an additional VIP: subnet=%(subnet)s '
                      'ip_address=%(ip)s', {'subnet': add_vip.subnet_id,
                                            'ip': add_vip.ip_address})
        return (vip.to_dict(),
                [additional_vip.to_dict()
                 for additional_vip in additional_vips])

    def revert(self, result, loadbalancer, *args, **kwargs):
        """Handle a failure to allocate vip."""

        if isinstance(result, failure.Failure):
            LOG.exception("Unable to allocate VIP")
            return
        vip, additional_vips = result
        vip = data_models.Vip(**vip)
        LOG.warning("Deallocating vip %s", vip.ip_address)
        try:
            self.network_driver.deallocate_vip(vip)
        except Exception as e:
            LOG.error("Failed to deallocate VIP.  Resources may still "
                      "be in use from vip: %(vip)s due to error: %(except)s",
                      {'vip': vip.ip_address, 'except': str(e)})


class AllocateVIPforFailover(AllocateVIP):
    """Task to allocate/validate the VIP for a failover flow."""

    def revert(self, result, loadbalancer, *args, **kwargs):
        """Handle a failure to allocate vip."""

        if isinstance(result, failure.Failure):
            LOG.exception("Unable to allocate VIP")
            return
        vip, additional_vips = result
        vip = data_models.Vip(**vip)
        LOG.info("Failover revert is not deallocating vip %s because this is "
                 "a failover.", vip.ip_address)


class DeallocateVIP(BaseNetworkTask):
    """Task to deallocate a VIP."""

    def execute(self, loadbalancer):
        """Deallocate a VIP."""

        LOG.debug("Deallocating a VIP %s", loadbalancer[constants.VIP_ADDRESS])

        # NOTE(blogan): this is kind of ugly but sufficient for now.  Drivers
        # will need access to the load balancer that the vip is/was attached
        # to.  However the data model serialization for the vip does not give a
        # backref to the loadbalancer if accessed through the loadbalancer.
        session = db_apis.get_session()
        with session.begin():
            db_lb = self.loadbalancer_repo.get(
                session, id=loadbalancer[constants.LOADBALANCER_ID])
            vip = db_lb.vip
            vip.load_balancer = db_lb
        self.network_driver.deallocate_vip(vip)


class UpdateVIP(BaseNetworkTask):
    """Task to update a VIP."""

    def execute(self, listeners):
        session = db_apis.get_session()
        with session.begin():
            loadbalancer = self.loadbalancer_repo.get(
                session, id=listeners[0][constants.LOADBALANCER_ID])

        LOG.debug("Updating VIP of load_balancer %s.", loadbalancer.id)

        self.network_driver.update_vip(loadbalancer)


class UpdateVIPForDelete(BaseNetworkTask):
    """Task to update a VIP for listener delete flows."""

    def execute(self, loadbalancer_id):
        session = db_apis.get_session()
        with session.begin():
            loadbalancer = self.loadbalancer_repo.get(
                session, id=loadbalancer_id)
        LOG.debug("Updating VIP for listener delete on load_balancer %s.",
                  loadbalancer.id)
        self.network_driver.update_vip(loadbalancer, for_delete=True)


class GetAmphoraNetworkConfigs(BaseNetworkTask):
    """Task to retrieve amphora network details."""

    def execute(self, loadbalancer, amphora=None):
        LOG.debug("Retrieving vip network details.")
        session = db_apis.get_session()
        with session.begin():
            db_amp = self.amphora_repo.get(session,
                                           id=amphora.get(constants.ID))
            db_lb = self.loadbalancer_repo.get(
                session, id=loadbalancer[constants.LOADBALANCER_ID])
        db_configs = self.network_driver.get_network_configs(
            db_lb, amphora=db_amp)
        provider_dict = {}
        for amp_id, amp_conf in db_configs.items():
            # Do not serialize loadbalancer class.  It's unused later and
            # could be ignored for storing in results of task in persistence DB
            provider_dict[amp_id] = amp_conf.to_dict(
                recurse=True, calling_classes=[data_models.LoadBalancer]
            )
        return provider_dict


class GetAmphoraNetworkConfigsByID(BaseNetworkTask):
    """Task to retrieve amphora network details."""

    def execute(self, loadbalancer_id, amphora_id=None):
        LOG.debug("Retrieving vip network details.")
        session = db_apis.get_session()
        with session.begin():
            loadbalancer = self.loadbalancer_repo.get(session,
                                                      id=loadbalancer_id)
            amphora = self.amphora_repo.get(session, id=amphora_id)
        db_configs = self.network_driver.get_network_configs(loadbalancer,
                                                             amphora=amphora)
        provider_dict = {}
        for amp_id, amp_conf in db_configs.items():
            # Do not serialize loadbalancer class.  It's unused later and
            # could be ignored for storing in results of task in persistence DB
            provider_dict[amp_id] = amp_conf.to_dict(
                recurse=True, calling_classes=[data_models.LoadBalancer]
            )
        return provider_dict


class GetAmphoraeNetworkConfigs(BaseNetworkTask):
    """Task to retrieve amphorae network details."""

    def execute(self, loadbalancer_id):
        LOG.debug("Retrieving vip network details.")
        session = db_apis.get_session()
        with session.begin():
            db_lb = self.loadbalancer_repo.get(
                session, id=loadbalancer_id)
        db_configs = self.network_driver.get_network_configs(db_lb)
        provider_dict = {}
        for amp_id, amp_conf in db_configs.items():
            # Do not serialize loadbalancer class.  It's unused later and
            # could be ignored for storing in results of task in persistence DB
            provider_dict[amp_id] = amp_conf.to_dict(
                recurse=True, calling_classes=[data_models.LoadBalancer]
            )
        return provider_dict


class RetrievePortIDsOnAmphoraExceptLBNetwork(BaseNetworkTask):
    """Task retrieving all the port ids on an amphora, except lb network."""

    def execute(self, amphora):
        LOG.debug("Retrieve all but the lb network port id on amphora %s.",
                  amphora[constants.ID])

        interfaces = self.network_driver.get_plugged_networks(
            compute_id=amphora[constants.COMPUTE_ID])

        ports = []
        for interface_ in interfaces:
            if interface_.port_id not in ports:
                port = self.network_driver.get_port(port_id=interface_.port_id)
                ips = port.fixed_ips
                lb_network = False
                for ip in ips:
                    if ip.ip_address == amphora[constants.LB_NETWORK_IP]:
                        lb_network = True
                if not lb_network:
                    ports.append(port)

        return ports


class PlugPorts(BaseNetworkTask):
    """Task to plug neutron ports into a compute instance."""

    def execute(self, amphora, ports):
        session = db_apis.get_session()
        with session.begin():
            db_amp = self.amphora_repo.get(session,
                                           id=amphora[constants.ID])
        for port in ports:
            LOG.debug('Plugging port ID: %(port_id)s into compute instance: '
                      '%(compute_id)s.',
                      {constants.PORT_ID: port.id,
                       constants.COMPUTE_ID: amphora[constants.COMPUTE_ID]})
            self.network_driver.plug_port(db_amp, port)


class ApplyQos(BaseNetworkTask):
    """Apply Quality of Services to the VIP"""

    def _apply_qos_on_vrrp_ports(self, loadbalancer, amps_data, qos_policy_id,
                                 is_revert=False, request_qos_id=None):
        """Call network driver to apply QoS Policy on the vrrp ports."""

        session = db_apis.get_session()
        with session.begin():
            if not amps_data:
                db_lb = self.loadbalancer_repo.get(
                    session,
                    id=loadbalancer[constants.LOADBALANCER_ID])
                amps_data = db_lb.amphorae

            amps_data = [amp
                         for amp in amps_data
                         if amp.status == constants.AMPHORA_ALLOCATED]

        apply_qos = ApplyQosAmphora()
        for amp_data in amps_data:
            apply_qos._apply_qos_on_vrrp_port(loadbalancer, amp_data.to_dict(),
                                              qos_policy_id)

    def execute(self, loadbalancer, amps_data=None, update_dict=None):
        """Apply qos policy on the vrrp ports which are related with vip."""
        session = db_apis.get_session()
        with session.begin():
            db_lb = self.loadbalancer_repo.get(
                session,
                id=loadbalancer[constants.LOADBALANCER_ID])

        qos_policy_id = db_lb.vip.qos_policy_id
        if not qos_policy_id and (
            not update_dict or (
                'vip' not in update_dict or
                'qos_policy_id' not in update_dict[constants.VIP])):
            return
        if update_dict and update_dict.get(constants.VIP):
            vip_dict = update_dict[constants.VIP]
            if vip_dict.get(constants.QOS_POLICY_ID):
                qos_policy_id = vip_dict[constants.QOS_POLICY_ID]

        self._apply_qos_on_vrrp_ports(loadbalancer, amps_data, qos_policy_id)

    def revert(self, result, loadbalancer, amps_data=None, update_dict=None,
               *args, **kwargs):
        """Handle a failure to apply QoS to VIP"""

        request_qos_id = loadbalancer['vip_qos_policy_id']
        orig_lb = self.task_utils.get_current_loadbalancer_from_db(
            loadbalancer[constants.LOADBALANCER_ID])
        orig_qos_id = orig_lb.vip.qos_policy_id
        if request_qos_id != orig_qos_id:
            self._apply_qos_on_vrrp_ports(loadbalancer, amps_data, orig_qos_id,
                                          is_revert=True,
                                          request_qos_id=request_qos_id)


class ApplyQosAmphora(BaseNetworkTask):
    """Apply Quality of Services to the VIP"""

    def _apply_qos_on_vrrp_port(self, loadbalancer, amp_data, qos_policy_id,
                                is_revert=False, request_qos_id=None):
        """Call network driver to apply QoS Policy on the vrrp ports."""
        try:
            self.network_driver.apply_qos_on_port(
                qos_policy_id,
                amp_data[constants.VRRP_PORT_ID])
        except Exception:
            if not is_revert:
                raise
            LOG.warning('Failed to undo qos policy %(qos_id)s '
                        'on vrrp port: %(port)s from '
                        'amphorae: %(amp)s',
                        {'qos_id': request_qos_id,
                         'port': amp_data[constants.VRRP_PORT_ID],
                         'amp': [amp.get(constants.ID) for amp in amp_data]})

    def execute(self, loadbalancer, amp_data=None, update_dict=None):
        """Apply qos policy on the vrrp ports which are related with vip."""
        qos_policy_id = loadbalancer['vip_qos_policy_id']
        if not qos_policy_id and (
            update_dict and (
                'vip' not in update_dict or
                'qos_policy_id' not in update_dict[constants.VIP])):
            return
        self._apply_qos_on_vrrp_port(loadbalancer, amp_data, qos_policy_id)

    def revert(self, result, loadbalancer, amp_data=None, update_dict=None,
               *args, **kwargs):
        """Handle a failure to apply QoS to VIP"""
        try:
            request_qos_id = loadbalancer['vip_qos_policy_id']
            orig_lb = self.task_utils.get_current_loadbalancer_from_db(
                loadbalancer[constants.LOADBALANCER_ID])
            orig_qos_id = orig_lb.vip.qos_policy_id
            if request_qos_id != orig_qos_id:
                self._apply_qos_on_vrrp_port(loadbalancer, amp_data,
                                             orig_qos_id, is_revert=True,
                                             request_qos_id=request_qos_id)
        except Exception as e:
            LOG.error('Failed to remove QoS policy: %s from port: %s due '
                      'to error: %s', orig_qos_id,
                      amp_data[constants.VRRP_PORT_ID], str(e))


class DeletePort(BaseNetworkTask):
    """Task to delete a network port."""

    @tenacity.retry(retry=tenacity.retry_if_exception_type(),
                    stop=tenacity.stop_after_attempt(
                        CONF.networking.max_retries),
                    wait=tenacity.wait_exponential(
                        multiplier=CONF.networking.retry_backoff,
                        min=CONF.networking.retry_interval,
                        max=CONF.networking.retry_max), reraise=True)
    def execute(self, port_id, passive_failure=False):
        """Delete the network port."""
        if port_id is None:
            return
        # tenacity 8.5.0 moves statistics from the retry object to the function
        try:
            retry_statistics = self.execute.statistics
        except AttributeError:
            retry_statistics = self.execute.retry.statistics

        if retry_statistics.get(constants.ATTEMPT_NUMBER, 1) == 1:
            LOG.debug("Deleting network port %s", port_id)
        else:
            LOG.warning('Retrying network port %s delete attempt %s of %s.',
                        port_id,
                        retry_statistics[constants.ATTEMPT_NUMBER],
                        self.execute.retry.stop.max_attempt_number)
        # Let the Taskflow engine know we are working and alive
        # Don't use get with a default for 'attempt_number', we need to fail
        # if that number is missing.
        self.update_progress(
            retry_statistics[constants.ATTEMPT_NUMBER] /
            self.execute.retry.stop.max_attempt_number)
        try:
            self.network_driver.delete_port(port_id)
        except Exception:
            if (retry_statistics[constants.ATTEMPT_NUMBER] !=
                    self.execute.retry.stop.max_attempt_number):
                LOG.warning('Network port delete for port id: %s failed. '
                            'Retrying.', port_id)
                raise
            if passive_failure:
                LOG.exception('Network port delete for port ID: %s failed. '
                              'This resource will be abandoned and should '
                              'manually be cleaned up once the '
                              'network service is functional.', port_id)
                # Let's at least attempt to disable it so if the instance
                # comes back from the dead it doesn't conflict with anything.
                try:
                    self.network_driver.admin_down_port(port_id)
                    LOG.info('Successfully disabled (admin down) network port '
                             '%s that failed to delete.', port_id)
                except Exception:
                    LOG.warning('Attempt to disable (admin down) network port '
                                '%s failed. The network service has failed. '
                                'Continuing.', port_id)
            else:
                LOG.exception('Network port delete for port ID: %s failed. '
                              'The network service has failed. '
                              'Aborting and reverting.', port_id)
                raise


class DeleteAmphoraMemberPorts(BaseNetworkTask):
    """Task to delete all of the member ports on an Amphora."""

    def execute(self, amphora_id, passive_failure=False):
        delete_port = DeletePort()
        session = db_apis.get_session()

        with session.begin():
            ports = self.amphora_member_port_repo.get_port_ids(
                session, amphora_id)
        for port in ports:
            delete_port.execute(port, passive_failure)
            with session.begin():
                self.amphora_member_port_repo.delete(session, port_id=port)


class CreateVIPBasePort(BaseNetworkTask):
    """Task to create the VIP base port for an amphora."""

    @tenacity.retry(retry=tenacity.retry_if_exception_type(),
                    stop=tenacity.stop_after_attempt(
                        CONF.networking.max_retries),
                    wait=tenacity.wait_exponential(
                        multiplier=CONF.networking.retry_backoff,
                        min=CONF.networking.retry_interval,
                        max=CONF.networking.retry_max), reraise=True)
    def execute(self, vip, vip_sg_id, amphora_id, additional_vips):
        port_name = constants.AMP_BASE_PORT_PREFIX + amphora_id
        fixed_ips = [{constants.SUBNET_ID: vip[constants.SUBNET_ID]}]
        sg_ids = []
        # NOTE(gthiemonge) clarification:
        # - vip_sg_id is the ID of the SG created and managed by Octavia.
        # - vip['sg_ids'] are the IDs of the SGs provided by the user.
        if vip_sg_id:
            sg_ids = [vip_sg_id]
        if vip["sg_ids"]:
            sg_ids += vip["sg_ids"]
        secondary_ips = [vip[constants.IP_ADDRESS]]
        for add_vip in additional_vips:
            secondary_ips.append(add_vip[constants.IP_ADDRESS])
        port = self.network_driver.create_port(
            vip[constants.NETWORK_ID], name=port_name, fixed_ips=fixed_ips,
            secondary_ips=secondary_ips,
            security_group_ids=sg_ids,
            qos_policy_id=vip[constants.QOS_POLICY_ID])
        LOG.info('Created port %s with ID %s for amphora %s',
                 port_name, port.id, amphora_id)
        return port.to_dict(recurse=True)

    def revert(self, result, vip, vip_sg_id, amphora_id, additional_vips,
               *args, **kwargs):
        if isinstance(result, failure.Failure):
            return
        try:
            port_name = constants.AMP_BASE_PORT_PREFIX + amphora_id
            self.network_driver.delete_port(result[constants.ID])
            LOG.info('Deleted port %s with ID %s for amphora %s due to a '
                     'revert.', port_name, result[constants.ID], amphora_id)
        except Exception as e:
            LOG.error('Failed to delete port %s. Resources may still be in '
                      'use for a port intended for amphora %s due to error '
                      '%s. Search for a port named %s',
                      result, amphora_id, str(e), port_name)


class AdminDownPort(BaseNetworkTask):

    def execute(self, port_id):
        try:
            self.network_driver.set_port_admin_state_up(port_id, False)
        except base.PortNotFound:
            return
        for i in range(CONF.networking.max_retries):
            port = self.network_driver.get_port(port_id)
            if port.status == constants.DOWN:
                LOG.debug('Disabled port: %s', port_id)
                return
            LOG.debug('Port %s is %s instead of DOWN, waiting.',
                      port_id, port.status)
            time.sleep(CONF.networking.retry_interval)
        LOG.error('Port %s failed to go DOWN. Port status is still %s. '
                  'Ignoring and continuing.', port_id, port.status)

    def revert(self, result, port_id, *args, **kwargs):
        if isinstance(result, failure.Failure):
            return
        try:
            self.network_driver.set_port_admin_state_up(port_id, True)
        except Exception as e:
            LOG.error('Failed to bring port %s admin up on revert due to: %s.',
                      port_id, str(e))


class GetVIPSecurityGroupID(BaseNetworkTask):

    def execute(self, loadbalancer_id):
        sg_name = utils.get_vip_security_group_name(loadbalancer_id)
        try:
            security_group = self.network_driver.get_security_group(sg_name)
            if security_group:
                return security_group.id
        except base.SecurityGroupNotFound:
            with excutils.save_and_reraise_exception() as ctxt:
                if self.network_driver.sec_grp_enabled:
                    LOG.error('VIP security group %s was not found.', sg_name)
                else:
                    ctxt.reraise = False
        return None


class CreateSRIOVBasePort(BaseNetworkTask):
    """Task to create a SRIOV base port for an amphora."""

    @tenacity.retry(retry=tenacity.retry_if_exception_type(),
                    stop=tenacity.stop_after_attempt(
                        CONF.networking.max_retries),
                    wait=tenacity.wait_exponential(
                        multiplier=CONF.networking.retry_backoff,
                        min=CONF.networking.retry_interval,
                        max=CONF.networking.retry_max), reraise=True)
    def execute(self, loadbalancer, amphora, subnet):
        session = db_apis.get_session()
        with session.begin():
            db_lb = self.loadbalancer_repo.get(
                session, id=loadbalancer[constants.LOADBALANCER_ID])
        port_name = constants.AMP_BASE_PORT_PREFIX + amphora[constants.ID]
        fixed_ips = [{constants.SUBNET_ID: subnet[constants.ID]}]
        addl_vips = [obj.ip_address for obj in db_lb.additional_vips]
        addl_vips.append(loadbalancer[constants.VIP_ADDRESS])
        port = self.network_driver.create_port(
            loadbalancer[constants.VIP_NETWORK_ID],
            name=port_name, fixed_ips=fixed_ips,
            secondary_ips=addl_vips,
            qos_policy_id=loadbalancer[constants.VIP_QOS_POLICY_ID],
            vnic_type=constants.VNIC_TYPE_DIRECT)
        LOG.info('Created port %s with ID %s for amphora %s',
                 port_name, port.id, amphora[constants.ID])
        return port.to_dict(recurse=True)

    def revert(self, result, loadbalancer, amphora, subnet, *args, **kwargs):
        if isinstance(result, failure.Failure):
            return
        try:
            port_name = constants.AMP_BASE_PORT_PREFIX + amphora['id']
            self.network_driver.delete_port(result[constants.ID])
            LOG.info('Deleted port %s with ID %s for amphora %s due to a '
                     'revert.', port_name, result[constants.ID], amphora['id'])
        except Exception as e:
            LOG.error('Failed to delete port %s. Resources may still be in '
                      'use for a port intended for amphora %s due to error '
                      '%s. Search for a port named %s',
                      result, amphora['id'], str(e), port_name)


class BuildAMPData(BaseNetworkTask):
    """Glue task to store the AMP_DATA dict from netork port information."""

    def execute(self, loadbalancer, amphora, port_data):
        amphora[constants.HA_IP] = loadbalancer[constants.VIP_ADDRESS]
        amphora[constants.HA_PORT_ID] = loadbalancer[constants.VIP_PORT_ID]
        amphora[constants.VRRP_ID] = 1
        amphora[constants.VRRP_PORT_ID] = port_data[constants.ID]
        amphora[constants.VRRP_IP] = port_data[
            constants.FIXED_IPS][0][constants.IP_ADDRESS]
        return amphora
