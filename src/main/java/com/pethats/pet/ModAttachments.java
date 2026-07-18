package com.pethats.pet;

import com.pethats.PetHats;
import net.fabricmc.fabric.api.attachment.v1.AttachmentRegistry;
import net.fabricmc.fabric.api.attachment.v1.AttachmentType;
import net.minecraft.resources.Identifier;

/** Data attachments used by Pet Hats. The worn hat itself lives in the vanilla {@code HEAD} equipment slot. */
public final class ModAttachments {

	/** The Ender Crown's pet inventory contents. Only meaningful while an Ender Crown is worn. */
	public static final AttachmentType<PetInventoryContainer> PET_INVENTORY = AttachmentRegistry.createPersistent(
			Identifier.fromNamespaceAndPath(PetHats.MOD_ID, "pet_inventory"), PetInventoryContainer.CODEC);

	private ModAttachments() {
	}

	public static void init() {
	}
}
