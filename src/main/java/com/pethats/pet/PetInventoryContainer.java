package com.pethats.pet;

import com.mojang.serialization.Codec;
import com.mojang.serialization.codecs.RecordCodecBuilder;
import java.util.ArrayList;
import java.util.List;
import net.minecraft.world.SimpleContainer;
import net.minecraft.world.item.ItemStack;

/**
 * The Ender Crown's pet inventory: {@link #STORAGE_SLOTS} general storage slots plus one dedicated
 * {@link #WEAPON_SLOT}. Persisted on the wearer via a Fabric data attachment (see {@link ModAttachments}).
 */
public class PetInventoryContainer extends SimpleContainer {
	public static final int STORAGE_SLOTS = 6;
	public static final int WEAPON_SLOT = STORAGE_SLOTS;
	public static final int TOTAL_SLOTS = STORAGE_SLOTS + 1;

	public static final Codec<PetInventoryContainer> CODEC =
			SlotEntry.CODEC.listOf().xmap(PetInventoryContainer::fromEntries, PetInventoryContainer::toEntries);

	public PetInventoryContainer() {
		super(TOTAL_SLOTS);
	}

	public ItemStack getWeapon() {
		return getItem(WEAPON_SLOT);
	}

	/** True if every storage and weapon slot is empty. */
	public boolean isFullyEmpty() {
		for (int i = 0; i < TOTAL_SLOTS; i++) {
			if (!getItem(i).isEmpty()) {
				return false;
			}
		}
		return true;
	}

	private static PetInventoryContainer fromEntries(List<SlotEntry> entries) {
		PetInventoryContainer container = new PetInventoryContainer();
		for (SlotEntry entry : entries) {
			if (entry.slot() >= 0 && entry.slot() < TOTAL_SLOTS) {
				container.setItem(entry.slot(), entry.stack());
			}
		}
		return container;
	}

	private static List<SlotEntry> toEntries(PetInventoryContainer container) {
		List<SlotEntry> entries = new ArrayList<>();
		for (int i = 0; i < TOTAL_SLOTS; i++) {
			ItemStack stack = container.getItem(i);
			if (!stack.isEmpty()) {
				entries.add(new SlotEntry(i, stack));
			}
		}
		return entries;
	}

	// A sparse (slot index, stack) pair, so persistence doesn't depend on how a codec treats empty stacks
	// inside a dense list.
	private record SlotEntry(int slot, ItemStack stack) {
		static final Codec<SlotEntry> CODEC = RecordCodecBuilder.create(instance -> instance
				.group(
						Codec.INT.fieldOf("slot").forGetter(SlotEntry::slot),
						ItemStack.CODEC.fieldOf("item").forGetter(SlotEntry::stack))
				.apply(instance, SlotEntry::new));
	}
}
