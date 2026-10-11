// pghadr: the smallest Azure setup that can show a real HA failover and a real
// cross-region DR promote.
//
// Creates, in the target resource group:
//   * Azure Database for PostgreSQL Flexible Server, General Purpose
//     Standard_D2ds_v5, PostgreSQL 16, 32 GiB, zone-redundant HA
//     (primary in one zone, synchronous standby in another)
//   * a firewall rule for one client IP and a `pghadr` database
//   * optionally (deployReplica=true) a read replica in a second region,
//     with its own firewall rule
//
// HA needs General Purpose or Memory Optimized; read replicas too. Both are
// billed while they run (see infra/README.md). Delete the resource group the
// same day: scripts/down.sh.
//
// Deploy with scripts/up.sh, or:
//   az deployment group create -g <rg> -f infra/main.bicep \
//     -p clientIp=<your ip> adminPassword=<secret>

targetScope = 'resourceGroup'

@description('Region for the primary. Must offer zone-redundant HA for your subscription (az postgres flexible-server list-skus -l <region>).')
param location string = resourceGroup().location

@description('Globally unique name for the primary server.')
@minLength(3)
@maxLength(63)
param serverName string = 'pghadr-${uniqueString(resourceGroup().id)}'

@description('Administrator login.')
param adminLogin string = 'pghadradmin'

@description('Administrator password.')
@secure()
@minLength(12)
param adminPassword string

@description('Your public IP address, allowed through the firewall.')
param clientIp string

@description('Compute size. HA and replicas need General Purpose or Memory Optimized.')
@allowed([
  'Standard_D2ds_v5'
  'Standard_D4ds_v5'
])
param skuName string = 'Standard_D2ds_v5'

@description('PostgreSQL major version.')
@allowed(['15', '16', '17'])
param postgresVersion string = '16'

@description('HA mode. ZoneRedundant puts the standby in another zone; SameZone keeps it in the same zone.')
@allowed(['ZoneRedundant', 'SameZone', 'Disabled'])
param haMode string = 'ZoneRedundant'

@description('Zone for the primary.')
param primaryZone string = '1'

@description('Zone for the standby (ZoneRedundant only).')
param standbyZone string = '2'

@description('Create a read replica in replicaLocation for the DR drill.')
param deployReplica bool = false

@description('Region for the read replica. Pick one your subscription may use (az postgres flexible-server list-skus -l <region>).')
param replicaLocation string = 'westus3'

@description('Name of the read replica.')
param replicaName string = '${serverName}-dr'

@description('Name of the application database.')
param databaseName string = 'pghadr'

@description('Tags for every resource; the lab sweeper deletes groups past their expires tag.')
param tags object = {
  project: 'mvp-lab'
  app: 'pghadr'
}

var tier = 'GeneralPurpose'

resource primary 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: tier
  }
  properties: {
    version: postgresVersion
    administratorLogin: adminLogin
    administratorLoginPassword: adminPassword
    availabilityZone: primaryZone
    storage: {
      storageSizeGB: 32
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: haMode == 'Disabled'
      ? { mode: 'Disabled' }
      : {
          mode: haMode
          standbyAvailabilityZone: haMode == 'ZoneRedundant' ? standbyZone : primaryZone
        }
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource clientRule 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: primary
  name: 'client'
  properties: {
    startIpAddress: clientIp
    endIpAddress: clientIp
  }
}

resource db 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: primary
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource replica 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = if (deployReplica) {
  name: replicaName
  location: replicaLocation
  tags: tags
  sku: {
    name: skuName
    tier: tier
  }
  properties: {
    createMode: 'Replica'
    sourceServerResourceId: primary.id
  }
  dependsOn: [
    db
    clientRule
  ]
}

resource replicaRule 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (deployReplica) {
  parent: replica
  name: 'client'
  properties: {
    startIpAddress: clientIp
    endIpAddress: clientIp
  }
}

output serverName string = primary.name
output fqdn string = primary.properties.fullyQualifiedDomainName
output replicaName string = deployReplica ? replicaName : ''
output replicaFqdn string = deployReplica ? replica!.properties.fullyQualifiedDomainName : ''
output adminLogin string = adminLogin
output databaseName string = databaseName
