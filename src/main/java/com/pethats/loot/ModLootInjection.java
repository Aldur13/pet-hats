package com.pethats.loot;

import com.pethats.item.ModItems;
import net.fabricmc.fabric.api.loot.v3.LootTableEvents;
import net.minecraft.world.level.storage.loot.BuiltInLootTables;
import net.minecraft.world.level.storage.loot.LootPool;
import net.minecraft.world.level.storage.loot.entries.LootItem;
import net.minecraft.world.level.storage.loot.predicates.LootItemRandomChanceCondition;
import net.minecraft.world.level.storage.loot.providers.number.ConstantValue;

/**
 * Guarantees a Captain's Hat in every shipwreck treasure chest and a Piglin War Mask in every bastion
 * treasure chest, and gives every end city treasure chest a 33% chance at an Ender Crown.
 */
public final class ModLootInjection {

	private static final float ENDER_CROWN_CHANCE = 0.33f;

	private ModLootInjection() {
	}

	public static void init() {
		LootTableEvents.MODIFY.register((key, tableBuilder, source, registries) -> {
			if (key.equals(BuiltInLootTables.SHIPWRECK_TREASURE)) {
				tableBuilder.withPool(LootPool.lootPool()
						.setRolls(ConstantValue.exactly(1))
						.add(LootItem.lootTableItem(ModItems.CAPTAINS_HAT)));
			} else if (key.equals(BuiltInLootTables.BASTION_TREASURE)) {
				tableBuilder.withPool(LootPool.lootPool()
						.setRolls(ConstantValue.exactly(1))
						.add(LootItem.lootTableItem(ModItems.PIGLIN_WAR_MASK)));
			} else if (key.equals(BuiltInLootTables.END_CITY_TREASURE)) {
				tableBuilder.withPool(LootPool.lootPool()
						.setRolls(ConstantValue.exactly(1))
						.add(LootItem.lootTableItem(ModItems.ENDER_CROWN))
						.when(LootItemRandomChanceCondition.randomChance(ENDER_CROWN_CHANCE)));
			}
		});
	}
}
